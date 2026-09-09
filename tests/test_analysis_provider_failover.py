"""Analysis-only Primary→Fallback provider failover tests.

Covers the configuration contract (optional provider/model pair, fail
closed on partial config, dedicated fallback credential, endpoint
isolation), the shared request budget across every invocation surface
(plain invoke, structured output, tool binding, LCEL composition),
permanent-vs-transient switching, per-invocation state under concurrency,
secret-free audit events, and the graph wiring that gives Decision the
same failover route as Analysis while the legacy paths stay untouched.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.retry import (
    FailoverRetryingLLM,
    ProviderFailure,
    RetryingLLM,
)
from tradingagents.llm_clients.roles import (
    RoleConfigError,
    describe_roles,
    resolve_role_config,
)

SLEEP_NONE = lambda _: None  # noqa: E731


def _base_config(**overrides):
    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "llm_provider": "openai",
            "deep_think_llm": "gpt-5.4-mini",
            "quick_think_llm": "gpt-5.4-nano",
            "backend_url": None,
            "data_cache_dir": tempfile.mkdtemp(),
            "results_dir": tempfile.mkdtemp(),
            "memory_log_path": str(Path(tempfile.mkdtemp()) / "memory.md"),
            "agent_memory_dir": tempfile.mkdtemp(),
        }
    )
    config.update(overrides)
    return config


class FakeRoute:
    """Scripted route stub: records prompts/schemas/tools, serves outcomes."""

    def __init__(self, tag, outcomes=()):
        self.tag = tag
        self.outcomes = list(outcomes)
        self.prompts = []
        self.schemas = []
        self.toolkits = []

    @property
    def calls(self):
        return len(self.prompts)

    def invoke(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(content=outcome, additional_kwargs={})

    def with_structured_output(self, schema, **kwargs):
        self.schemas.append(schema)
        return self

    def bind_tools(self, tools, **kwargs):
        self.toolkits.append(tools)
        return self


def _failover(primary_outcomes, fallback_outcomes, max_retries=3, on_switch=None):
    primary = FakeRoute("primary", primary_outcomes)
    fallback = FakeRoute("fallback", fallback_outcomes)
    llm = FailoverRetryingLLM(
        primary,
        fallback,
        role="analysis",
        primary_provider="openai",
        primary_model="muse-spark-1.3",
        fallback_provider="openrouter",
        fallback_model="muse-spark-1.3",
        max_retries=max_retries,
        on_switch=on_switch,
    )
    llm._controller.sleep = SLEEP_NONE
    return llm, primary, fallback


class FallbackConfigDefaultTests(unittest.TestCase):
    def test_default_config_ships_fallback_keys_disabled(self):
        self.assertIsNone(DEFAULT_CONFIG["analysis_fallback_provider"])
        self.assertIsNone(DEFAULT_CONFIG["analysis_fallback_model"])
        self.assertIsNone(DEFAULT_CONFIG["analysis_fallback_backend_url"])


class FallbackRoleResolutionTests(unittest.TestCase):
    def test_no_fallback_keys_leave_failover_disabled(self):
        resolved = resolve_role_config(
            _base_config(analysis_provider="openai", analysis_model="gpt-5.4-mini")
        )
        self.assertIsNone(resolved["analysis_fallback"])
        self.assertEqual(resolved["analysis_fallback_api_key"], "")

    def test_fallback_keys_alone_enter_roles_mode_and_enable_failover(self):
        # Hermetic: OPENAI_USE_LOCAL=true in a developer's .env rewrites the
        # global provider to local_openai and would fail this assertion.
        with patch.dict(
            os.environ,
            {"ANALYSIS_FALLBACK_OPENAI_API_KEY": "fb-key", "OPENAI_USE_LOCAL": ""},
        ):
            resolved = resolve_role_config(
                _base_config(
                    analysis_fallback_provider="openai",
                    analysis_fallback_model="gpt-5.4-mini",
                )
            )
        self.assertEqual(resolved["mode"], "roles")
        # Primary Analysis still resolves from the global provider/model.
        self.assertEqual(resolved["analysis"].provider, "openai")
        self.assertEqual(resolved["analysis"].model, "gpt-5.4-mini")
        fallback = resolved["analysis_fallback"]
        self.assertEqual(fallback.role, "analysis_fallback")
        self.assertEqual(fallback.provider, "openai")
        self.assertEqual(fallback.model, "gpt-5.4-mini")
        self.assertEqual(resolved["analysis_fallback_api_key"], "fb-key")

    def test_partial_fallback_configs_fail_closed(self):
        for kwargs in (
            {"analysis_fallback_provider": "openrouter"},
            {"analysis_fallback_model": "muse-spark-1.3"},
            {"analysis_fallback_backend_url": "https://fallback.example/v1"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RoleConfigError):
                    resolve_role_config(
                        _base_config(
                            analysis_provider="openai",
                            analysis_model="gpt-5.4-mini",
                            **kwargs,
                        )
                    )

    def test_invalid_fallback_provider_is_error_not_fallback(self):
        with self.assertRaises(RoleConfigError):
            resolve_role_config(
                _base_config(
                    analysis_provider="openai",
                    analysis_model="gpt-5.4-mini",
                    analysis_fallback_provider="gpt5",
                    analysis_fallback_model="m",
                )
            )

    def test_same_provider_fallback_inherits_primary_endpoint(self):
        resolved = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="muse-spark-1.3",
                analysis_backend_url="https://provider-a.example/v1",
                analysis_fallback_provider="openai",
                analysis_fallback_model="muse-spark-1.3",
            )
        )
        self.assertEqual(
            resolved["analysis_fallback"].backend_url,
            "https://provider-a.example/v1",
        )

    def test_cross_provider_fallback_never_inherits_primary_endpoint(self):
        resolved = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="muse-spark-1.3",
                analysis_backend_url="https://provider-a.example/v1",
                analysis_fallback_provider="openrouter",
                analysis_fallback_model="muse-spark-1.3",
            )
        )
        self.assertIsNone(resolved["analysis_fallback"].backend_url)

    def test_explicit_fallback_endpoint_wins_and_display_strips_secrets(self):
        resolved = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="muse-spark-1.3",
                analysis_fallback_provider="openai",
                analysis_fallback_model="muse-spark-1.3",
                analysis_fallback_backend_url="https://provider-b.example/v1?api-key=secret",
            )
        )
        self.assertEqual(
            resolved["analysis_fallback"].display_endpoint(),
            "https://provider-b.example/v1",
        )
        self.assertNotIn("secret", describe_roles(resolved))

    def test_startup_summary_never_exposes_userinfo_or_query_secrets(self):
        # Acceptance §24: the role summary must never leak userinfo
        # (user:password@), query-string credentials or fragments — on any
        # line, including the new AnalysisFallback surface.
        resolved = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="muse-spark-1.3",
                analysis_backend_url=(
                    "https://user:password@example.com/v1?api_key=SECRET#fragment"
                ),
                analysis_fallback_provider="openrouter",
                analysis_fallback_model="muse-spark-1.3",
                analysis_fallback_backend_url=(
                    "https://user2:password2@fallback.example/v1"
                    "?api_key=SECRET2#frag2"
                ),
            )
        )
        self.assertEqual(
            resolved["analysis"].display_endpoint(), "https://***@example.com/v1"
        )
        self.assertEqual(
            resolved["analysis_fallback"].display_endpoint(),
            "https://***@fallback.example/v1",
        )
        summary = describe_roles(resolved)
        for leaked in (
            "password",
            "password2",
            "user:",
            "user2:",
            "SECRET",
            "SECRET2",
            "fragment",
            "frag2",
        ):
            self.assertNotIn(leaked, summary)
        # The redaction marker is visible so operators know credentials
        # existed on the endpoint (same idiom as retry.sanitize_error).
        self.assertIn("***@example.com/v1", summary)
        self.assertIn("***@fallback.example/v1", summary)

    def test_fallback_credential_override_precedes_provider_standard_key(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "standard-openrouter",
                "ANALYSIS_FALLBACK_OPENROUTER_API_KEY": "fallback-only",
                "ANALYSIS_OPENAI_API_KEY": "analysis-primary",
            },
        ):
            resolved = resolve_role_config(
                _base_config(
                    analysis_provider="openai",
                    analysis_model="muse-spark-1.3",
                    analysis_fallback_provider="openrouter",
                    analysis_fallback_model="muse-spark-1.3",
                )
            )
        self.assertEqual(resolved["analysis_fallback_api_key"], "fallback-only")
        # Primary keeps its own credential; nothing is copied across routes.
        self.assertEqual(resolved["analysis_api_key"], "analysis-primary")

    def test_fallback_credential_falls_back_to_provider_standard_key(self):
        with patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "standard-openrouter"}, clear=True
        ):
            resolved = resolve_role_config(
                _base_config(
                    analysis_provider="openai",
                    analysis_model="muse-spark-1.3",
                    analysis_fallback_provider="openrouter",
                    analysis_fallback_model="muse-spark-1.3",
                )
            )
        self.assertEqual(resolved["analysis_fallback_api_key"], "standard-openrouter")

    def test_describe_roles_mentions_fallback_only_when_configured(self):
        without = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="gpt-5.4-mini",
                decision_provider="openai",
                decision_model="gpt-5.4-mini",
            )
        )
        self.assertNotIn("AnalysisFallback", describe_roles(without))
        with_fb = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="muse-spark-1.3",
                analysis_fallback_provider="openrouter",
                analysis_fallback_model="muse-spark-1.3",
            )
        )
        self.assertIn(
            "AnalysisFallback=openrouter/muse-spark-1.3", describe_roles(with_fb)
        )


class SharedBudgetFailoverTests(unittest.TestCase):
    """Exact attempt counting through the shared Primary/Fallback budget:
    at most 1 + max_retries total requests per logical invocation."""

    def test_primary_success_never_touches_fallback(self):
        llm, primary, fallback = _failover(["ok"], [])
        self.assertEqual(llm.invoke("hello").content, "ok")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_primary_transient_failure_switches_next_attempt_to_fallback(self):
        llm, primary, fallback = _failover([TimeoutError("timed out")], ["ok"])
        self.assertEqual(llm.invoke("hello").content, "ok")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_remaining_attempts_stay_on_fallback(self):
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")],
            [ConnectionError("connection reset"), TimeoutError("timed out"), "ok"],
        )
        self.assertEqual(llm.invoke("hello").content, "ok")
        # Primary → Fallback → Fallback → Fallback; never back to Primary.
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 3)

    def test_shared_budget_exhaustion_raises_provider_failure(self):
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")],
            [
                RuntimeError("503 service unavailable"),
                RuntimeError("429 too many requests"),
                TimeoutError("timed out"),
            ],
        )
        with self.assertRaises(ProviderFailure) as ctx:
            llm.invoke("hello")
        self.assertEqual(ctx.exception.attempts, 4)
        self.assertEqual(ctx.exception.category, "transient")
        # The failure is reported against the route that failed last.
        self.assertEqual(ctx.exception.provider, "openrouter")
        self.assertEqual(ctx.exception.model, "muse-spark-1.3")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 3)

    def test_permanent_primary_failure_never_fails_over(self):
        llm, primary, fallback = _failover(
            [RuntimeError("401 unauthorized")], ["never"]
        )
        with self.assertRaises(ProviderFailure) as ctx:
            llm.invoke("hello")
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(ctx.exception.category, "permanent")
        self.assertEqual(ctx.exception.provider, "openai")
        self.assertEqual(fallback.calls, 0)

    def test_permanent_fallback_failure_stops_immediately(self):
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")], [RuntimeError("401 unauthorized"), "never"]
        )
        with self.assertRaises(ProviderFailure) as ctx:
            llm.invoke("hello")
        self.assertEqual(ctx.exception.attempts, 2)
        self.assertEqual(ctx.exception.category, "permanent")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_retries_zero_means_one_primary_request_even_with_fallback(self):
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")], ["never"], max_retries=0
        )
        with self.assertRaises(ProviderFailure) as ctx:
            llm.invoke("hello")
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(fallback.calls, 0)


class PerInvocationStateTests(unittest.TestCase):
    def test_next_invocation_starts_on_primary_again(self):
        llm, primary, fallback = _failover(
            ["ok-1", TimeoutError("timed out"), "ok-3"], ["ok-2"]
        )
        self.assertEqual(llm.invoke("a").content, "ok-1")
        self.assertEqual(llm.invoke("b").content, "ok-2")
        self.assertEqual(llm.invoke("c").content, "ok-3")
        self.assertEqual(primary.calls, 3)
        self.assertEqual(fallback.calls, 1)

    def test_parallel_invocations_do_not_share_failover_state(self):
        class Primary:
            def __init__(self):
                self.prompts = []

            def invoke(self, prompt, **kwargs):
                self.prompts.append(prompt)
                if prompt == "call-A":
                    raise TimeoutError("timed out")
                return SimpleNamespace(
                    content=f"primary:{prompt}", additional_kwargs={}
                )

        class Fallback:
            def __init__(self):
                self.prompts = []

            def invoke(self, prompt, **kwargs):
                self.prompts.append(prompt)
                return SimpleNamespace(
                    content=f"fallback:{prompt}", additional_kwargs={}
                )

        primary, fallback = Primary(), Fallback()
        llm = FailoverRetryingLLM(
            primary,
            fallback,
            role="analysis",
            primary_provider="openai",
            primary_model="m",
            fallback_provider="openrouter",
            fallback_model="m",
            max_retries=3,
        )
        llm._controller.sleep = SLEEP_NONE
        results = {}

        def run(tag):
            results[tag] = llm.invoke(tag)

        threads = [
            threading.Thread(target=run, args=("call-A",)),
            threading.Thread(target=run, args=("call-B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results["call-A"].content, "fallback:call-A")
        # B's healthy Primary must not be forced onto Fallback by A's outage.
        self.assertEqual(results["call-B"].content, "primary:call-B")
        self.assertEqual(fallback.prompts, ["call-A"])


class StructuredAndToolParityTests(unittest.TestCase):
    def test_structured_output_paths_share_one_budget_and_schema(self):
        schema = {"type": "object"}
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")], ["structured-ok"]
        )
        structured = llm.with_structured_output(schema)
        self.assertEqual(structured.invoke("prompt").content, "structured-ok")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        # Both route runnables are built with the same schema object.
        self.assertIs(primary.schemas[0], schema)
        self.assertIs(fallback.schemas[0], schema)

    def test_structured_output_exhaustion_raises_with_shared_cap(self):
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")],
            [TimeoutError("timed out")] * 3,
        )
        structured = llm.with_structured_output(dict)
        with self.assertRaises(ProviderFailure) as ctx:
            structured.invoke("prompt")
        self.assertEqual(ctx.exception.attempts, 4)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 3)

    def test_tool_bound_paths_share_one_budget_and_tools(self):
        tools = ["tool-a"]
        llm, primary, fallback = _failover(
            [ConnectionError("connection reset")], ["tools-ok"]
        )
        bound = llm.bind_tools(tools)
        self.assertEqual(bound.invoke("prompt").content, "tools-ok")
        self.assertIs(primary.toolkits[0], tools)
        self.assertIs(fallback.toolkits[0], tools)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_tool_bound_exhaustion_raises_with_shared_cap(self):
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")], [TimeoutError("timed out")] * 3
        )
        bound = llm.bind_tools([])
        with self.assertRaises(ProviderFailure) as ctx:
            bound.invoke("prompt")
        # Shared cap through LCEL: 1 Primary + 3 Fallback = 4 total.
        self.assertEqual(ctx.exception.attempts, 4)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 3)


class FailoverAuditEventTests(unittest.TestCase):
    def test_switch_event_emitted_once_per_invocation_with_sanitized_payload(self):
        events = []
        llm, primary, fallback = _failover(
            [TimeoutError("timed out")],
            [TimeoutError("timed out"), TimeoutError("timed out"), "ok"],
            on_switch=events.append,
        )
        self.assertEqual(llm.invoke("hello").content, "ok")
        # Exactly one event for the single switch, not one per fallback try.
        self.assertEqual(len(events), 1)
        payload = events[0]
        self.assertEqual(payload["role"], "analysis")
        self.assertEqual(payload["from_provider"], "openai")
        self.assertEqual(payload["from_model"], "muse-spark-1.3")
        self.assertEqual(payload["to_provider"], "openrouter")
        self.assertEqual(payload["to_model"], "muse-spark-1.3")
        self.assertEqual(payload["trigger_category"], "transient")
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["max_requests_per_invocation"], 4)

    def test_no_event_when_primary_succeeds(self):
        events = []
        llm, _, _ = _failover(["ok"], ["unused"], on_switch=events.append)
        llm.invoke("hello")
        self.assertEqual(events, [])

    def test_no_event_for_permanent_primary_failure(self):
        events = []
        llm, _, _ = _failover(
            [RuntimeError("401 unauthorized")], ["unused"], on_switch=events.append
        )
        with self.assertRaises(ProviderFailure):
            llm.invoke("hello")
        self.assertEqual(events, [])

    def test_default_audit_hook_writes_to_run_audit_logger(self):
        recorded = []

        class StubLogger:
            def log_event(self, event_type, symbol=None, run_id=None, payload=None):
                recorded.append((event_type, payload))

        llm, _, _ = _failover([TimeoutError("timed out")], ["ok"])
        with patch(
            "tradingagents.run_logger.get_run_audit_logger",
            return_value=StubLogger(),
        ):
            llm.invoke("hello")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0], "llm_provider_failover")
        self.assertNotIn("api_key", recorded[0][1])

    def test_audit_logging_failure_never_affects_execution(self):
        def broken(payload):
            raise RuntimeError("audit sink down")

        llm, _, _ = _failover([TimeoutError("timed out")], ["ok"], on_switch=broken)
        self.assertEqual(llm.invoke("hello").content, "ok")


class RunLogPersistenceTests(unittest.TestCase):
    """A real RunAuditLogger persists exactly one failover event on the
    active run — the operator-facing proof, with no secrets in the log."""

    def _restore_cwd(self):
        os.chdir(self._old_cwd)
        self._tmp_cwd.cleanup()

    def test_failover_event_persists_to_the_active_run_log(self):
        import json

        from tradingagents.run_logger import RunAuditLogger

        self._old_cwd = os.getcwd()
        self._tmp_cwd = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp_cwd.name)
        self.addCleanup(self._restore_cwd)

        logger = RunAuditLogger()
        logger.start_run(symbol="NVDA", trade_date="2026-09-05", config={"x": 1})

        llm, primary, fallback = _failover([TimeoutError("timed out")], ["ok"])
        with patch(
            "tradingagents.run_logger.get_run_audit_logger", return_value=logger
        ):
            self.assertEqual(llm.invoke("hello").content, "ok")

        logger.finish_run(symbol="NVDA", status="completed")

        runs_dir = Path("eval_results") / "NVDA" / "TradingAgentsStrategy_logs" / "runs"
        files = sorted(runs_dir.glob("*.json"))
        self.assertTrue(files, "expected a run log file")
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        events = [
            event
            for event in payload.get("events", [])
            if event.get("type") == "llm_provider_failover"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["to_provider"], "openrouter")
        self.assertEqual(events[0]["payload"]["attempt"], 2)


class LcelCompositionTests(unittest.TestCase):
    """The failover wrapper must stay a LangChain Runnable so existing
    ``prompt | llm`` chains keep composing (Phase B acceptance finding)."""

    def test_prompt_pipe_failover_llm_composes_and_fails_over(self):
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.runnables import RunnableSequence

        llm, primary, fallback = _failover(
            [TimeoutError("timed out")], ["final answer"]
        )
        chain = ChatPromptTemplate.from_messages([("human", "{q}")]) | llm
        self.assertIsInstance(chain, RunnableSequence)
        self.assertEqual(chain.invoke({"q": "hello"}).content, "final answer")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_prompt_pipe_tool_bound_failover_composes_and_counts(self):
        from langchain_core.prompts import ChatPromptTemplate

        llm, primary, fallback = _failover(
            [TimeoutError("timed out")],
            [TimeoutError("timed out")] * 2,
            max_retries=2,
        )
        chain = ChatPromptTemplate.from_messages([("human", "{q}")]) | llm.bind_tools(
            []
        )
        with self.assertRaises(ProviderFailure) as ctx:
            chain.invoke({"q": "hello"})
        # Shared cap through LCEL: 1 Primary + 2 Fallback = 3 total.
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 2)


class GraphWiringTests(unittest.TestCase):
    """Only the Analysis role gains failover; Decision keeps its own plain
    retry owner, and both fallback route clients are actually constructed."""

    def _build(self, config):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        captured = []

        class FakeInner:
            def __init__(self, tag):
                self.tag = tag

            def with_structured_output(self, schema, **kwargs):
                return self

            def bind_tools(self, tools, **kwargs):
                return self

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                captured.append(kwargs)

            def get_llm(self):
                return FakeInner(f"{self.kwargs['provider']}:{self.kwargs['model']}")

        with patch(
            "tradingagents.graph.trading_graph.create_llm_client",
            side_effect=lambda **kwargs: FakeClient(**kwargs),
        ):
            graph = TradingAgentsGraph(
                selected_analysts=["market"], config=config, debug=False
            )
        return graph, captured

    def test_fallback_config_builds_failover_wrapper_for_analysis_and_decision(self):
        with patch.dict(
            os.environ, {"ANALYSIS_FALLBACK_OPENROUTER_API_KEY": "fb-secret"}
        ):
            graph, captured = self._build(
                _base_config(
                    analysis_provider="openai",
                    analysis_model="muse-spark-1.3",
                    analysis_fallback_provider="openrouter",
                    analysis_fallback_model="muse-spark-1.3",
                    decision_provider="openai",
                    decision_model="decision-model-y",
                )
            )
        analysis_client = graph.graph_setup.quick_thinking_llm
        self.assertIsInstance(analysis_client, FailoverRetryingLLM)
        # Every research node stays on the single failover-wrapped client.
        self.assertIs(graph.graph_setup.deep_thinking_llm, analysis_client)
        self.assertEqual(analysis_client.inner.tag, "openai:muse-spark-1.3")
        self.assertEqual(analysis_client.fallback_inner.tag, "openrouter:muse-spark-1.3")
        policy = analysis_client.retry_policy
        self.assertTrue(policy["failover_enabled"])
        self.assertEqual(policy["max_requests_per_invocation"], 4)
        self.assertEqual(policy["fallback_provider"], "openrouter")
        # Decision consumes the same fallback route: the Risk Manager is the
        # last call of every ticker, so quota exhaustion there must switch
        # to the fallback instead of stopping a Phase-D round.
        decision_client = graph.graph_setup.decision_llm
        self.assertIsInstance(decision_client, FailoverRetryingLLM)
        self.assertEqual(decision_client.inner.tag, "openai:decision-model-y")
        self.assertEqual(decision_client.fallback_inner.tag, "openrouter:muse-spark-1.3")
        decision_policy = decision_client.retry_policy
        self.assertTrue(decision_policy["failover_enabled"])
        self.assertEqual(decision_policy["role"], "decision")
        self.assertEqual(decision_policy["fallback_provider"], "openrouter")
        # Both the Primary and Fallback route clients were constructed.
        routes = {(c["provider"], c["model"]) for c in captured}
        self.assertIn(("openrouter", "muse-spark-1.3"), routes)
        self.assertIn(("openai", "muse-spark-1.3"), routes)
        self.assertIn(("openai", "decision-model-y"), routes)

    def test_no_fallback_config_keeps_plain_retrying_llm(self):
        graph, captured = self._build(
            _base_config(
                analysis_provider="openai",
                analysis_model="analysis-model-x",
            )
        )
        analysis_client = graph.graph_setup.quick_thinking_llm
        self.assertIsInstance(analysis_client, RetryingLLM)
        self.assertNotIsInstance(analysis_client, FailoverRetryingLLM)
        self.assertIs(graph.graph_setup.deep_thinking_llm, analysis_client)
        self.assertEqual(graph.graph_setup.decision_llm.inner.tag, "openai:analysis-model-x")
        routes = {(c["provider"], c["model"]) for c in captured}
        self.assertNotIn(("openrouter", "analysis-model-x"), routes)


if __name__ == "__main__":
    unittest.main()
