"""Phase B1 tests: fixed Analysis/Decision roles, legacy compatibility,
credential/endpoint isolation, and UI/CLI-facing config resolution."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.roles import (
    RoleConfigError,
    RoleSpec,
    describe_roles,
    resolve_role_config,
)


def _base_config(**overrides):
    config = DEFAULT_CONFIG.copy()
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


class RoleResolutionTests(unittest.TestCase):
    def test_no_role_keys_keeps_legacy_mode(self):
        resolved = resolve_role_config(_base_config())
        self.assertEqual(resolved["mode"], "legacy")
        self.assertIn("legacy", describe_roles(resolved))

    def test_empty_strings_count_as_unset(self):
        resolved = resolve_role_config(
            _base_config(
                analysis_provider="  ",
                analysis_model="",
                decision_backend_url="",
            )
        )
        self.assertEqual(resolved["mode"], "legacy")

    def test_any_role_key_enters_roles_mode_with_defaults(self):
        resolved = resolve_role_config(_base_config(decision_provider="openai"))
        self.assertEqual(resolved["mode"], "roles")
        analysis: RoleSpec = resolved["analysis"]
        decision: RoleSpec = resolved["decision"]
        # Analysis provider/model default to llm_provider/deep_think_llm.
        self.assertEqual(analysis.provider, "openai")
        self.assertEqual(analysis.model, "gpt-5.4-mini")
        # Decision defaults to the resolved Analysis role.
        self.assertEqual(decision.provider, "openai")
        self.assertEqual(decision.model, analysis.model)

    def test_analysis_role_override_and_inheritance(self):
        resolved = resolve_role_config(
            _base_config(analysis_provider="openai", analysis_model="gpt-5.4-nano")
        )
        self.assertEqual(resolved["analysis"].model, "gpt-5.4-nano")
        self.assertEqual(resolved["decision"].model, "gpt-5.4-nano")

    def test_cross_provider_decision_without_model_is_config_error(self):
        with self.assertRaises(RoleConfigError):
            resolve_role_config(
                _base_config(
                    analysis_provider="openai",
                    analysis_model="gpt-5.4-mini",
                    decision_provider="anthropic",
                )
            )

    def test_cross_provider_analysis_without_model_is_config_error(self):
        with self.assertRaises(RoleConfigError):
            resolve_role_config(_base_config(analysis_provider="google"))

    def test_cross_provider_decision_with_model_resolves_isolated_endpoint(self):
        resolved = resolve_role_config(
            _base_config(
                analysis_provider="openai",
                analysis_model="gpt-5.4-mini",
                analysis_backend_url="https://openai-proxy.example/v1?api-key=secret",
                decision_provider="anthropic",
                decision_model="claude-sonnet-4-6",
            )
        )
        decision = resolved["decision"]
        self.assertEqual(decision.provider, "anthropic")
        self.assertEqual(decision.model, "claude-sonnet-4-6")
        # Cross-provider roles never inherit the other role's endpoint.
        self.assertIsNone(decision.backend_url)
        # Display endpoint strips query secrets.
        self.assertNotIn("secret", resolved["analysis"].display_endpoint())

    def test_invalid_provider_is_error_not_fallback(self):
        with self.assertRaises(RoleConfigError):
            resolve_role_config(_base_config(analysis_provider="gpt5"))

    def test_explicit_role_provider_not_rewritten_by_local_switch(self):
        with patch.dict(os.environ, {"OPENAI_USE_LOCAL": "1"}):
            resolved = resolve_role_config(
                _base_config(
                    analysis_provider="openai",
                    analysis_model="gpt-5.4-mini",
                    decision_provider="openai",
                    decision_model="gpt-5.4-mini",
                )
            )
        self.assertEqual(resolved["analysis"].provider, "openai")

    def test_global_local_switch_applies_to_defaulted_provider(self):
        with patch.dict(os.environ, {"OPENAI_USE_LOCAL": "1"}):
            resolved = resolve_role_config(_base_config(decision_model="local-model"))
        self.assertEqual(resolved["analysis"].provider, "local_openai")
        self.assertEqual(resolved["decision"].provider, "local_openai")

    def test_role_specific_key_env_overrides_provider_key(self):
        with patch.dict(
            os.environ,
            {"DECISION_OPENAI_API_KEY": "decision-only-key", "OPENAI_API_KEY": "shared-key"},
        ):
            resolved = resolve_role_config(_base_config(decision_provider="openai"))
        self.assertEqual(resolved["decision_api_key"], "decision-only-key")
        self.assertEqual(resolved["analysis_api_key"], "shared-key")

    def test_decision_key_defaults_to_provider_key_not_analysis_copy(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "shared-key"}, clear=True):
            resolved = resolve_role_config(_base_config(decision_provider="openai"))
        self.assertEqual(resolved["decision_api_key"], "shared-key")
        self.assertEqual(resolved["analysis_api_key"], "shared-key")


class RoleGraphWiringTests(unittest.TestCase):
    """The resolved roles must reach the graph: one Analysis client for every
    research node, and the Decision client only on the Risk Manager."""

    def _build(self, config):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        captured = []
        clients = {}

        class FakeInner:
            def __init__(self, tag):
                self.tag = tag

            def invoke(self, _prompt, **kwargs):
                raise AssertionError("not used in wiring test")

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

        def fake_factory(**kwargs):
            client = FakeClient(**kwargs)
            clients[kwargs.get("model")] = client
            return client

        with patch(
            "tradingagents.graph.trading_graph.create_llm_client",
            side_effect=fake_factory,
        ):
            graph = TradingAgentsGraph(
                selected_analysts=["market", "news"],
                config=config,
                debug=False,
            )
        return graph, captured

    def test_roles_mode_analysis_and_decision_clients_are_distinct(self):
        graph, captured = self._build(
            _base_config(
                analysis_provider="openai",
                analysis_model="analysis-model-x",
                decision_provider="openai",
                decision_model="decision-model-y",
            )
        )
        models = [c["model"] for c in captured]
        self.assertIn("analysis-model-x", models)
        self.assertIn("decision-model-y", models)
        # Every research node stays on the Analysis client...
        self.assertIs(graph.graph_setup.quick_thinking_llm, graph.graph_setup.deep_thinking_llm)
        self.assertEqual(graph.graph_setup.quick_thinking_llm.inner.tag, "openai:analysis-model-x")
        # ...while only the Risk Manager gets the Decision client.
        self.assertEqual(graph.graph_setup.decision_llm.inner.tag, "openai:decision-model-y")

    def test_legacy_mode_decision_injection_point_stays_none(self):
        graph, captured = self._build(_base_config())
        self.assertEqual(graph.graph_setup.decision_llm, None)
        models = {c["model"] for c in captured}
        self.assertEqual(models, {"gpt-5.4-mini", "gpt-5.4-nano"})


class RoleKwargsIsolationTests(unittest.TestCase):
    def test_provider_kwargs_do_not_leak_between_roles(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        captured = []

        class FakeInner:
            def with_structured_output(self, schema, **kwargs):
                return self

            def bind_tools(self, tools, **kwargs):
                return self

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                captured.append(kwargs)

            def get_llm(self):
                return FakeInner()

        config = _base_config(
            analysis_provider="google",
            analysis_model="gemini-2.5-flash",
            decision_provider="openai",
            decision_model="gpt-5.4-mini",
            google_thinking_level="high",
            openai_reasoning_effort="medium",
        )
        with patch(
            "tradingagents.graph.trading_graph.create_llm_client",
            side_effect=lambda **kwargs: FakeClient(**kwargs),
        ):
            TradingAgentsGraph(selected_analysts=["market"], config=config, debug=False)

        by_model = {c["model"]: c for c in captured}
        self.assertIn("thinking_level", by_model["gemini-2.5-flash"])
        self.assertNotIn("reasoning_effort", by_model["gemini-2.5-flash"])
        self.assertIn("reasoning_effort", by_model["gpt-5.4-mini"])
        self.assertNotIn("thinking_level", by_model["gpt-5.4-mini"])


if __name__ == "__main__":
    unittest.main()
