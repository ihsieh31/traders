"""Phase B2 tests: single bounded retry owner, exact request caps per
adapter family, permanent-vs-transient classification, structured/tool
paths, and whole-run stop semantics on provider failure."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.agents.utils.structured import (
    invoke_risk_structured_strict,
    invoke_structured_or_freetext,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.retry import (
    ProviderFailure,
    RetryingLLM,
    classify_provider_error,
    validate_llm_max_retries,
)

SLEEP_NONE = lambda _: None  # noqa: E731


def _retrying(max_retries=3, **kwargs):
    return RetryingLLM(SimpleNamespace(), max_retries=max_retries, **kwargs)


class RetryPolicyValidationTests(unittest.TestCase):
    def test_llm_max_retries_accepts_only_integers_0_to_3(self):
        for value, ok in ((0, True), (1, True), (2, True), (3, True), (-1, False),
                          (4, False), (1.5, False), ("3", False), (True, False)):
            with self.subTest(value=value):
                if ok:
                    self.assertEqual(validate_llm_max_retries(value), value)
                else:
                    with self.assertRaises(ValueError):
                        validate_llm_max_retries(value)

    def test_error_classification_prefers_status_then_markers(self):
        class WithStatus(Exception):
            pass

        self.assertEqual(classify_provider_error(WithStatus("x")), "permanent")
        exc = WithStatus("x")
        exc.status_code = 429
        self.assertEqual(classify_provider_error(exc), "transient")
        exc.status_code = 401
        self.assertEqual(classify_provider_error(exc), "permanent")
        exc.status_code = 503
        self.assertEqual(classify_provider_error(exc), "transient")
        self.assertEqual(classify_provider_error(TimeoutError("timed out")), "transient")
        self.assertEqual(
            classify_provider_error(ConnectionError("connection reset")), "transient"
        )
        self.assertEqual(classify_provider_error(ValueError("401 unauthorized")), "permanent")

    def test_provider_failure_message_is_sanitized(self):
        failure = ProviderFailure(
            role="decision", provider="openai", model="gpt-x", attempts=2,
            category="permanent",
            detail="401 unauthorized for key sk-proj-abcdef123456 base=https://api.example.com/v1?api_key=zzz",
        )
        self.assertNotIn("sk-proj-abcdef123456", str(failure))
        self.assertNotIn("zzz", str(failure))
        self.assertEqual(failure.attempts, 2)
        self.assertEqual(failure.to_dict()["role"], "decision")


class RetryOwnerCountingTests(unittest.TestCase):
    """Exact attempt counting through the single retry owner. SDK-level
    retries are pinned to 0 by the adapters; these tests prove the owner's
    cap: first try + at most N retries = at most N+1 requests."""

    def _owner(self, calls, outcomes, max_retries=3):
        def call():
            calls.append(1)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        llm = _retrying(role="analysis", provider="openai", model="m", max_retries=max_retries)
        llm._controller.sleep = SLEEP_NONE
        return llm, call

    def test_three_transient_then_success_is_four_requests(self):
        calls, outcomes = [], [TimeoutError("t1"), ConnectionError("c1"),
                               RuntimeError("429 too many requests"), "ok"]
        llm, call = self._owner(calls, outcomes)
        self.assertEqual(llm._controller.run(call), "ok")
        self.assertEqual(len(calls), 4)

    def test_four_transient_failures_have_no_fifth_request(self):
        calls, outcomes = [], [ConnectionError("x")] * 4
        llm, call = self._owner(calls, outcomes)
        with self.assertRaises(ProviderFailure) as ctx:
            llm._controller.run(call)
        self.assertEqual(len(calls), 4)
        self.assertEqual(ctx.exception.attempts, 4)
        self.assertEqual(ctx.exception.category, "transient")

    def test_retry_zero_means_exactly_one_request(self):
        calls, outcomes = [], [TimeoutError("t")]
        llm, call = self._owner(calls, outcomes, max_retries=0)
        with self.assertRaises(ProviderFailure):
            llm._controller.run(call)
        self.assertEqual(len(calls), 1)

    def test_permanent_failure_stops_immediately_with_real_attempt_count(self):
        calls, outcomes = [], [RuntimeError("401 unauthorized"), "never"]
        llm, call = self._owner(calls, outcomes)
        with self.assertRaises(ProviderFailure) as ctx:
            llm._controller.run(call)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(ctx.exception.category, "permanent")

    def test_exhaustion_reports_real_attempts_not_hardcoded(self):
        calls, outcomes = [], [TimeoutError("t"), TimeoutError("t")]
        llm, call = self._owner(calls, outcomes, max_retries=1)
        with self.assertRaises(ProviderFailure) as ctx:
            llm._controller.run(call)
        self.assertEqual(ctx.exception.attempts, 2)


class AdapterFamilyTransportTests(unittest.TestCase):
    """Per-family transport counting with the real SDK stack where the
    installed packages allow an injectable transport."""

    def test_openai_family_counts_http_requests_with_mock_transport(self):
        import httpx

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(1)
            if len(requests) < 4:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-4.1-mini",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        from langchain_openai import ChatOpenAI

        inner = ChatOpenAI(
            model="gpt-4.1-mini",
            api_key="test-key",
            max_retries=0,  # SDK layer pinned: our owner is the only retry
            timeout=5.0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        llm = _retrying(role="analysis", provider="openai", model="gpt-4.1-mini", max_retries=3)
        llm._controller.sleep = SLEEP_NONE
        llm._inner = inner
        result = llm.invoke("hello")
        self.assertEqual(result.content, "ok")
        # 3 transient HTTP 429s + 1 success = exactly 4 requests.
        self.assertEqual(len(requests), 4)

        # Permanent 401: exactly one HTTP request, no retry.
        requests.clear()

        def handler_401(request: httpx.Request) -> httpx.Response:
            requests.append(1)
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        inner_401 = ChatOpenAI(
            model="gpt-4.1-mini",
            api_key="test-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler_401)),
        )
        llm_401 = _retrying(role="analysis", provider="openai", model="gpt-4.1-mini", max_retries=3)
        llm_401._controller.sleep = SLEEP_NONE
        llm_401._inner = inner_401
        with self.assertRaises(ProviderFailure) as ctx:
            llm_401.invoke("hello")
        self.assertEqual(len(requests), 1)
        self.assertEqual(ctx.exception.category, "permanent")

    def test_anthropic_family_counts_sdk_requests(self):
        import anthropic

        calls = []

        class FakeResponse(SimpleNamespace):
            def model_dump(self):
                return {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(1)
                if len(calls) < 4:
                    raise anthropic.APIConnectionError(
                        "connection error", httpx_request=None
                    )
                return FakeResponse(
                    content=[SimpleNamespace(type="text", text="ok")],
                    role="assistant",
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

        class FakeAnthropicClient:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

        from tradingagents.llm_clients.anthropic_client import AnthropicClient

        # langchain-anthropic builds anthropic.Client(**params); patching the
        # client constructor keeps the whole path offline.
        with patch("anthropic.Client", side_effect=lambda **kw: FakeAnthropicClient(**kw)):
            client = AnthropicClient("claude-sonnet-4-6", api_key="test-key")
            llm = _retrying(role="analysis", provider="anthropic", model="claude-sonnet-4-6")
            llm._controller.sleep = SLEEP_NONE
            llm._inner = client.get_llm()
            result = llm.invoke("hello")
        self.assertIn("ok", str(result.content))
        self.assertEqual(len(calls), 4)

        # Constructed adapter must pin the SDK retry layer to 0. _client is
        # a lazy cached_property, so touch it to force construction.
        with patch("anthropic.Client", side_effect=lambda **kw: FakeAnthropicClient(**kw)) as anthropic_cls:
            inner = AnthropicClient("claude-sonnet-4-6", api_key="test-key").get_llm()
            _ = inner._client
            self.assertEqual(anthropic_cls.call_args.kwargs.get("max_retries"), 0)

    def test_google_family_counts_sdk_requests(self):
        from google.api_core.exceptions import ServiceUnavailable

        calls = []

        def fake_generate_content(self, *args, **kwargs):
            calls.append(1)
            if len(calls) < 4:
                raise ServiceUnavailable("backend error")
            from google.ai.generativelanguage_v1beta.types import (
                Candidate, Content, GenerateContentResponse, Part,
            )

            return GenerateContentResponse(
                candidates=[
                    Candidate(
                        content=Content(role="model", parts=[Part(text="ok")]),
                        finish_reason=1,
                    )
                ]
            )

        from google.ai.generativelanguage_v1beta.services.generative_service.client import (
            GenerativeServiceClient,
        )
        from tradingagents.llm_clients.google_client import GoogleClient

        with patch.object(
            GenerativeServiceClient, "generate_content", fake_generate_content
        ):
            client = GoogleClient("gemini-2.5-flash", api_key="test-key", max_retries=0)
            llm = _retrying(role="analysis", provider="google", model="gemini-2.5-flash")
            llm._controller.sleep = SLEEP_NONE
            llm._inner = client.get_llm()
            result = llm.invoke("hello")
        self.assertIn("ok", str(result.content))
        # Exactly 4 SDK calls: tenacity's stop_after_attempt(0) adds none.
        self.assertEqual(len(calls), 4)

        # Constructed adapter must pin langchain's tenacity layer to 0
        # (its max_retries semantics are total-attempts and it retries
        # permanent GoogleAPIError subclasses).
        from tradingagents.llm_clients.google_client import NormalizedChatGoogleGenerativeAI

        with patch(
            "tradingagents.llm_clients.google_client.NormalizedChatGoogleGenerativeAI"
        ) as chat_cls:
            chat_cls.return_value = SimpleNamespace()
            GoogleClient("gemini-2.5-flash", api_key="test-key").get_llm()
            self.assertEqual(chat_cls.call_args.kwargs.get("max_retries"), 0)

    def test_azure_family_counts_http_requests_with_mock_transport(self):
        import httpx

        from langchain_openai import AzureChatOpenAI

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(1)
            return httpx.Response(
                502, json={"error": {"message": "bad gateway"}}
            )

        inner = AzureChatOpenAI(
            model="gpt-4.1-mini",
            azure_deployment="deploy-x",
            azure_endpoint="https://example.openai.azure.com",
            api_key="test-key",
            api_version="2024-12-01-preview",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        llm = _retrying(role="decision", provider="azure", model="deploy-x", max_retries=3)
        llm._controller.sleep = SLEEP_NONE
        llm._inner = inner
        with self.assertRaises(ProviderFailure) as ctx:
            llm.invoke("hello")
        # Exactly 4 requests, no fifth; Azure has no native key configured.
        self.assertEqual(len(requests), 4)
        self.assertEqual(ctx.exception.attempts, 4)

    def test_gpt5_responses_model_pins_sdk_retries_and_raises(self):
        from tradingagents.agents.utils.gpt5_llm import GPT5ChatModel

        model = GPT5ChatModel(model="gpt-5.4-mini", api_key="test-key")
        self.assertEqual(model._client.max_retries, 0)
        self.assertTrue(model.timeout is None)

        class ExplodingCompletions:
            responses = SimpleNamespace(
                create=SimpleNamespace(side_effect=None)
            )

        def boom(**kwargs):
            raise RuntimeError("503 service unavailable")

        exploded = SimpleNamespace(responses=SimpleNamespace(create=boom))
        object.__setattr__(model, "_client", exploded)
        with self.assertRaises(RuntimeError):
            # Must raise, never return the error text as normal content.
            model.invoke("hello")


class StructuredAndToolPathTests(unittest.TestCase):
    def test_structured_runnable_respects_the_same_cap(self):
        calls = []

        class FakeStructured:
            def invoke(self, _prompt, **kwargs):
                calls.append(1)
                raise TimeoutError("timed out")

        class FakeInner:
            def with_structured_output(self, schema, **kwargs):
                return FakeStructured()

        llm = _retrying(role="decision", provider="openai", model="m", max_retries=3)
        llm._controller.sleep = SLEEP_NONE
        llm._inner = FakeInner()
        structured = llm.with_structured_output(dict)
        with self.assertRaises(ProviderFailure):
            structured.invoke("prompt")
        self.assertEqual(len(calls), 4)

    def test_tool_bound_runnable_respects_the_same_cap(self):
        calls = []

        class FakeBound:
            def invoke(self, _prompt, **kwargs):
                calls.append(1)
                raise ConnectionError("connection reset")

        class FakeInner:
            def bind_tools(self, tools, **kwargs):
                return FakeBound()

        llm = _retrying(role="analysis", provider="openai", model="m", max_retries=2)
        llm._controller.sleep = SLEEP_NONE
        llm._inner = FakeInner()
        bound = llm.bind_tools([])
        with self.assertRaises(ProviderFailure):
            bound.invoke("prompt")
        self.assertEqual(len(calls), 3)

    def test_non_risk_free_text_fallback_does_not_catch_provider_error(self):
        class ProviderBroken:
            def invoke(self, _prompt, **kwargs):
                raise ProviderFailure(
                    role="analysis", provider="openai", model="m", attempts=4,
                    category="transient", detail="timeout",
                )

        class PlainLLM:
            def __init__(self):
                self.called = False

            def invoke(self, _prompt):
                self.called = True
                return SimpleNamespace(content="fallback")

        plain = PlainLLM()
        with self.assertRaises(ProviderFailure):
            invoke_structured_or_freetext(ProviderBroken(), plain, "p", lambda v: v, "Agent")
        self.assertFalse(plain.called)  # no second request issued

    def test_non_risk_fallback_still_covers_schema_failures(self):
        class SchemaBroken:
            def invoke(self, _prompt):
                raise ValueError("missing field")

        content = invoke_structured_or_freetext(
            SchemaBroken(),
            SimpleNamespace(invoke=lambda p: SimpleNamespace(content="plain text")),
            "p",
            lambda v: v,
            "Agent",
        )
        self.assertEqual(content, "plain text")

    def test_risk_strict_boundary_separates_provider_and_schema_failures(self):
        class ProviderBroken:
            def invoke(self, _prompt):
                raise ProviderFailure(
                    role="decision", provider="anthropic", model="c", attempts=4,
                    category="transient", detail="429",
                )

        with self.assertRaises(ProviderFailure):
            invoke_risk_structured_strict(
                ProviderBroken(), "p", lambda v: v, "Risk Manager", schema=dict
            )

        class SchemaBroken:
            def invoke(self, _prompt):
                from tradingagents.agents.schemas import RiskDecision

                return {"unexpected": "shape", "action": "BUY"}

        from tradingagents.agents.schemas import RiskDecision

        content, value, reason = invoke_risk_structured_strict(
            SchemaBroken(), "p", lambda v: v, "Risk Manager", schema=RiskDecision
        )
        self.assertIsNone(content)
        self.assertIn("structured_validation_failed", reason)

        # Schema errors must not trigger a repair request: the broken object
        # above is invoked exactly once by the strict boundary.
        broken_calls = []

        class CountingSchemaBroken:
            def invoke(self, _prompt):
                broken_calls.append(1)
                raise ValueError("bad schema")

        invoke_risk_structured_strict(
            CountingSchemaBroken(), "p", lambda v: v, "Risk Manager", schema=dict
        )
        self.assertEqual(len(broken_calls), 1)


class WholeRunStopTests(unittest.TestCase):
    def _graph(self, invoke_side_effect):
        import os
        import tempfile

        from tradingagents.graph.trading_graph import TradingAgentsGraph

        class FakeCompiled:
            def invoke(self, state, **kwargs):
                return invoke_side_effect(state)

        class FakeWorkflow:
            def compile(self, checkpointer=None):
                return FakeCompiled()

        # Run from a temp cwd so run logs land outside the repo.
        self._old_cwd = os.getcwd()
        self._tmp_cwd = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp_cwd.name)
        self.addCleanup(self._restore_cwd)

        config = dict(DEFAULT_CONFIG)
        config.update(
            {
                "data_cache_dir": "cache",
                "results_dir": "eval_results",
                "memory_log_path": "memory.md",
                "agent_memory_dir": "agent-memory",
                "checkpoint_enabled": False,
            }
        )
        with patch(
            "tradingagents.graph.trading_graph.GraphSetup.setup_graph",
            return_value=FakeWorkflow(),
        ), patch("tradingagents.graph.trading_graph.create_llm_client"):
            graph = TradingAgentsGraph(selected_analysts=["market"], config=config)
        return graph

    def _restore_cwd(self):
        import os

        os.chdir(self._old_cwd)
        self._tmp_cwd.cleanup()

    def _latest_run_payload(self, symbol):
        import json
        from pathlib import Path

        runs_dir = (
            Path("eval_results")
            / symbol
            / "TradingAgentsStrategy_logs"
            / "runs"
        )
        files = sorted(runs_dir.glob("*.json"))
        self.assertTrue(files, "expected a run log file")
        return json.loads(files[-1].read_text(encoding="utf-8"))

    def test_provider_failure_marks_run_stopped_and_reraises(self):
        failure = ProviderFailure(
            role="analysis", provider="openai", model="m", attempts=4,
            category="transient", detail="timeout",
        )

        def explode(state):
            raise failure

        graph = self._graph(explode)
        with self.assertRaises(ProviderFailure):
            graph.propagate("NVDA", "2026-09-05")

        # The run log must record a stopped run with identifiable detail.
        payload = self._latest_run_payload("NVDA")
        self.assertEqual(payload["status"], "stopped")
        provider_events = [
            e for e in payload.get("events", [])
            if e.get("type") == "provider_failure"
        ]
        self.assertTrue(provider_events)
        log_payload = provider_events[-1]["payload"]
        self.assertEqual(log_payload["role"], "analysis")
        self.assertEqual(log_payload["attempts"], 4)
        self.assertEqual(log_payload["category"], "transient")
        self.assertTrue(log_payload["run_stopped"])

    def test_non_provider_errors_still_report_failed(self):
        def explode(state):
            raise ValueError("some other bug")

        graph = self._graph(explode)
        with self.assertRaises(ValueError):
            graph.propagate("TSLA", "2026-09-05")
        payload = self._latest_run_payload("TSLA")
        self.assertEqual(payload["status"], "failed")


class ParallelCoordinatorStopTests(unittest.TestCase):
    """A provider failure inside parallel analysts/risk must abort the whole
    round: no downstream dispatch, unstarted work cancelled, no empty-report
    fallback swallowing the failure."""

    def _setup(self):
        from tradingagents.graph.conditional_logic import ConditionalLogic
        from tradingagents.graph.setup import GraphSetup

        return GraphSetup(
            None, None, None, {}, None, None, None, None, None,
            ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
            config={},
        )

    def test_parallel_analyst_provider_failure_propagates_and_cancels(self):
        setup = self._setup()
        calls = []

        def ok_analyst(state):
            calls.append("ok")
            return {**state, "market_report": "fine"}

        def broken_analyst(state):
            calls.append("broken")
            raise ProviderFailure(
                role="analysis", provider="openai", model="m", attempts=4,
                category="transient", detail="timeout",
            )

        coordinator = setup._create_parallel_analysts_coordinator(
            ["market", "news"],
            {"market": ok_analyst, "news": broken_analyst},
            {}, {},
        )
        state = {"company_of_interest": "NVDA", "messages": []}
        with self.assertRaises(ProviderFailure):
            coordinator(state)
        # Both analysts were dispatched, the failure propagated, and no
        # empty-report fallback masked it.
        self.assertIn("broken", calls)

    def test_parallel_risk_provider_failure_propagates(self):
        setup = self._setup()

        def broken(state):
            raise ProviderFailure(
                role="analysis", provider="openai", model="m", attempts=4,
                category="transient", detail="429",
            )

        coordinator = setup._create_parallel_risk_round_one_coordinator(
            {"Risky": broken, "Safe": broken, "Neutral": broken}
        )
        state = {
            "company_of_interest": "NVDA",
            "risk_debate_state": {"count": 0},
        }
        with self.assertRaises(ProviderFailure):
            coordinator(state)


class SchedulerStopTests(unittest.TestCase):
    def test_provider_stop_clears_queue_and_halts_scheduling(self):
        from webui.components.analysis import mark_provider_stop
        from webui.utils.state import app_state

        app_state.analysis_queue = ["AAPL", "MSFT"]
        failure = ProviderFailure(
            role="analysis", provider="openai", model="m", attempts=4,
            category="transient", detail="timeout",
        )
        mark_provider_stop("AAPL", failure)
        self.assertEqual(app_state.analysis_queue, [])
        self.assertIn("AAPL", app_state.provider_stop_reason)
        self.assertIn("restart", app_state.provider_stop_reason)

        from webui.callbacks.control_callbacks import _halt_scheduling_for_provider_stop

        app_state.stop_loop = False
        app_state.stop_market_hour = False
        _halt_scheduling_for_provider_stop()
        self.assertTrue(app_state.stop_loop)
        self.assertTrue(app_state.stop_market_hour)
        self.assertEqual(app_state.analysis_queue, [])

    def test_operator_restart_clears_provider_stop(self):
        from webui.utils.state import app_state

        app_state.provider_stop_reason = "stale"
        app_state.reset()
        self.assertIsNone(app_state.provider_stop_reason)


class LcelCompositionRegressionTests(unittest.TestCase):
    """Regression for the acceptance F1 finding: the retry wrappers must be
    LangChain Runnables, or every analyst's ``prompt | llm.bind_tools(tools)``
    chain raises TypeError before any request and the failure is swallowed
    into an empty report."""

    def _inner(self, calls, outcomes):
        from types import SimpleNamespace as NS

        class Inner:
            def bind_tools(self, tools, **kwargs):
                return self

            def with_structured_output(self, schema, **kwargs):
                return self

            def invoke(self, prompt, *args, **kwargs):
                calls.append(1)
                outcome = outcomes.pop(0) if outcomes else "ok"
                if isinstance(outcome, Exception):
                    raise outcome
                return NS(content=outcome, additional_kwargs={})

        return Inner()

    def test_prompt_pipe_retrying_llm_composes_and_counts(self):
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.runnables import RunnableSequence

        calls = []
        llm = RetryingLLM(
            self._inner(calls, [TimeoutError("t1"), ConnectionError("t2"),
                                TimeoutError("t3"), "final answer"]),
            role="analysis", provider="openai", model="m", max_retries=3,
        )
        llm._controller.sleep = SLEEP_NONE
        chain = ChatPromptTemplate.from_messages([("human", "{q}")]) | llm
        self.assertIsInstance(chain, RunnableSequence)
        result = chain.invoke({"q": "hello"})
        self.assertEqual(result.content, "final answer")
        self.assertEqual(len(calls), 4)  # retry owner still governs LCEL paths

    def test_prompt_pipe_tool_bound_runnable_composes_and_counts(self):
        from langchain_core.prompts import ChatPromptTemplate

        calls = []
        llm = RetryingLLM(
            self._inner(calls, [TimeoutError("t")] * 4),
            role="analysis", provider="openai", model="m", max_retries=2,
        )
        llm._controller.sleep = SLEEP_NONE
        chain = ChatPromptTemplate.from_messages([("human", "{q}")]) | llm.bind_tools([])
        with self.assertRaises(ProviderFailure) as ctx:
            chain.invoke({"q": "hello"})
        self.assertEqual(len(calls), 3)  # 1 + max_retries, no fifth request
        self.assertEqual(ctx.exception.attempts, 3)

    def test_real_market_analyst_node_runs_through_the_retrying_chain(self):
        """The exact production composition: the real market analyst node with
        the wrapped LLM must reach the provider and return real report content
        instead of dying at chain construction into an empty-report fallback."""
        from types import SimpleNamespace as NS
        from unittest.mock import patch

        from tradingagents.agents.analysts.market_analyst import create_market_analyst
        from tradingagents.agents.utils.agent_utils import Toolkit

        calls = []

        class Inner:
            def bind_tools(self, tools, **kwargs):
                return self

            def invoke(self, prompt, *args, **kwargs):
                calls.append(1)
                return NS(
                    content="NVDA technical narrative. FINAL TRANSACTION PROPOSAL: **HOLD**",
                    additional_kwargs={},
                )

        llm = RetryingLLM(
            Inner(), role="analysis", provider="openai", model="m", max_retries=3
        )
        llm._controller.sleep = SLEEP_NONE
        toolkit = SimpleNamespace(
            config={
                "online_tools": False,
                "max_tool_iterations_per_agent": 8,
                "max_same_tool_call_repeats": 1,
            },
            has_alpaca_credentials=lambda: False,
            get_stockstats_indicators_report=Toolkit.get_stockstats_indicators_report,
        )
        node = create_market_analyst(llm, toolkit)
        state = {
            "company_of_interest": "NVDA",
            "trade_date": "2026-09-05",
            "messages": [],
        }
        with patch(
            "tradingagents.agents.analysts.market_analyst.capture_agent_prompt",
            side_effect=lambda *a, **k: None,
        ):
            out = node(state)
        self.assertIn("NVDA technical narrative", out["market_report"])
        self.assertIn("FINAL TRANSACTION PROPOSAL", out["market_report"])
        self.assertEqual(len(calls), 1)  # healthy provider: exactly one request


if __name__ == "__main__":
    unittest.main()
