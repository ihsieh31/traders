"""Regression tests for the Phase D / OpenAI-compatible integration fixes.

Covers:
- P0-1 preflight secrets boundary: injected probes only ever see "***";
  the default probe re-resolves the live key per exact role and fails
  closed when no key can be resolved.
- P0-2 per-role probe routes (no cross-role dedupe) including a configured
  analysis_fallback route.
- P1-1 analysis_fallback_* persistence/runtime/resolve chain.
- P1-2 CLI optional fallback setup (decline, enable, preserve, no secrets).
- P1-3 neutral User-Agent on custom OpenAI-compatible endpoints while the
  official OpenAI endpoint keeps SDK defaults.
- P2 hermetic env isolation for Phase B role tests.

All tests are offline: no probe reaches the network.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents import long_run as lr
from tradingagents.agents.utils.gpt5_llm import (
    _NEUTRAL_USER_AGENT,
    GPT5ChatModel,
    get_chat_model,
)
from tradingagents.llm_clients.retry import ProviderFailure

REDACTED = lr.REDACTED_API_KEY

_ROLE_KEY_VARS = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_USE_LOCAL", "OPENAI_BASE_URL",
    "ANALYSIS_OPENAI_API_KEY", "DECISION_OPENAI_API_KEY",
    "SCREENING_OPENAI_API_KEY", "ANALYSIS_FALLBACK_OPENAI_API_KEY",
)


def _hermetic_env(**extra):
    """Ambient env scrubbed of local-switch and credential overrides."""
    env = dict(os.environ)
    for key in _ROLE_KEY_VARS:
        env.pop(key, None)
    env.update(extra)
    return env


def _all_role_keys(**extra):
    """Deterministic role credentials for a full preflight run."""
    env = _hermetic_env(
        ANALYSIS_OPENAI_API_KEY="sk-key-analysis",
        DECISION_OPENAI_API_KEY="sk-key-decision",
        SCREENING_OPENAI_API_KEY="sk-key-screening",
        ANALYSIS_FALLBACK_OPENAI_API_KEY="sk-key-fallback",
    )
    env.pop("OPENAI_API_KEY", None)
    env.update(extra)
    return env


def _valid_cfg(**overrides):
    cfg = lr.default_long_run_config()
    cfg.update({
        "duration_calendar_days": 30,
        "run_time_et": "11:00",
        "base_trade_notional_usd": 1000.0,
        "analysts": ["market", "news"],
        "research_depth": 3,
        "output_language": "English",
        "analysis_provider": "openai",
        "analysis_model": "gpt-fake-analysis",
        "decision_provider": "openai",
        "decision_model": "gpt-fake-decision",
        "screening_provider": "openai",
        "screening_model": "gpt-fake-screening",
    })
    cfg.update(overrides)
    return cfg


class _RecordingProbe:
    """Injected probe double: records kwargs, returns a fixed payload."""

    def __init__(self, exc: Exception | None = None, fail_role: str | None = None):
        self.calls = []
        self.exc = exc
        self.fail_role = fail_role

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None and (
            self.fail_role is None or kwargs.get("role") == self.fail_role
        ):
            raise self.exc
        return {
            "role": kwargs.get("role"), "provider": kwargs.get("provider"),
            "model": kwargs.get("model"), "result": "ok",
        }


class _PreflightTest(unittest.TestCase):
    """Minimal scaffolding: preflight stops at the first probe failure, so
    broker/calendar/execution checks are fakes and no network is touched."""

    def setUp(self):
        import tempfile

        self.workdir = Path(tempfile.mkdtemp(prefix="preflight-"))
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.old_env = dict(os.environ)
        os.environ["TRADINGAGENTS_LONG_RUN_DIR"] = str(self.workdir / "longrun")
        # WebUI runtime keys are a process-global credential store; keep the
        # key resolution here fully driven by the env set per test.
        from tradingagents.dataflows import config as cfgmod

        self._cfgmod = cfgmod
        self._saved_runtime_keys = dict(cfgmod._runtime_api_keys)
        cfgmod._runtime_api_keys.clear()

    def tearDown(self):
        self._cfgmod._runtime_api_keys.clear()
        self._cfgmod._runtime_api_keys.update(self._saved_runtime_keys)
        os.chdir(self.old_cwd)
        os.environ.clear()
        os.environ.update(self.old_env)

        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)

    def _deps(self, probe):
        broker = SimpleNamespace(
            get_account=lambda: SimpleNamespace(equity=1.0, last_equity=1.0, cash=1.0, buying_power=1.0, id="acct"),
            get_all_positions=lambda: [],
        )
        service = SimpleNamespace(
            startup_recover=lambda: {"success": True, "reconciliation_reasons": []},
        )
        return lr.LongRunDeps(
            llm_probe_fn=probe,
            broker_client_factory=lambda: broker,
            calendar_client=SimpleNamespace(
                get_calendar=lambda request: [
                    SimpleNamespace(date="2026-09-08", open="09:30", close="16:00"),
                ],
            ),
            execution_service_factory=lambda: service,
            alert_fn=lambda subject, body, runtime: {"sent": False},
        )


class PreflightSecretsBoundaryTest(_PreflightTest):
    """A: the injected probe never sees a live API key."""

    def test_injected_probe_only_receives_redacted_marker(self):
        probe = _RecordingProbe()
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys(
            OPENAI_API_KEY="sk-live-standard-secret",
        )):
            lr.run_preflight(cfg, runtime, self._deps(probe))
        self.assertTrue(probe.calls)
        for call in probe.calls:
            self.assertEqual(call["api_key"], "***")
        joined = str(probe.calls)
        self.assertNotIn("sk-live-standard-secret", joined)
        self.assertNotIn("sk-key-analysis", joined)

    def test_preflight_result_and_checks_contain_no_secret(self):
        probe = _RecordingProbe()
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys()):
            result = lr.run_preflight(cfg, runtime, self._deps(probe))
        self.assertTrue(result["ok"])
        self.assertNotIn("sk-key-analysis", str(result))


class DefaultProbeLiveKeyResolutionTest(_PreflightTest):
    """B: the real probe re-resolves the live key per exact role."""

    def _run_default_probe_capture(self, env, role, provider="openai"):
        captured = {}

        def fake_create_llm_client(provider_, model, base_url=None,
                                   api_key=None, **kwargs):
            captured["api_key"] = api_key
            captured["provider"] = provider_

            class _Inner:
                def invoke(self, _prompt, **kw):
                    return "ok"

            class _Client:
                def get_llm(self):
                    return _Inner()

            return _Client()

        def fake_retrying(inner, **kwargs):
            captured["role"] = kwargs.get("role")

            class _LLM:
                def invoke(self, _prompt, **kw):
                    return "ok"

            return _LLM()

        with patch.dict(os.environ, env):
            with patch(
                "tradingagents.llm_clients.factory.create_llm_client",
                side_effect=fake_create_llm_client,
            ):
                with patch(
                    "tradingagents.llm_clients.retry.RetryingLLM",
                    side_effect=fake_retrying,
                ):
                    result = lr._default_llm_probe(
                        role=role, provider=provider, model="gpt-fake",
                        backend_url=None, api_key=REDACTED, max_retries=0,
                    )
        return result, captured

    def test_analysis_role_uses_analysis_key(self):
        _, captured = self._run_default_probe_capture(
            _hermetic_env(ANALYSIS_OPENAI_API_KEY="sk-role-analysis"),
            "analysis",
        )
        self.assertEqual(captured["api_key"], "sk-role-analysis")

    def test_decision_role_uses_decision_key(self):
        _, captured = self._run_default_probe_capture(
            _hermetic_env(
                ANALYSIS_OPENAI_API_KEY="sk-role-analysis",
                DECISION_OPENAI_API_KEY="sk-role-decision",
            ),
            "decision",
        )
        self.assertEqual(captured["api_key"], "sk-role-decision")

    def test_screening_role_uses_screening_key(self):
        _, captured = self._run_default_probe_capture(
            _hermetic_env(
                ANALYSIS_OPENAI_API_KEY="sk-role-analysis",
                SCREENING_OPENAI_API_KEY="sk-role-screening",
            ),
            "screening",
        )
        self.assertEqual(captured["api_key"], "sk-role-screening")

    def test_analysis_fallback_role_uses_fallback_key(self):
        _, captured = self._run_default_probe_capture(
            _hermetic_env(
                ANALYSIS_OPENAI_API_KEY="sk-role-analysis",
                ANALYSIS_FALLBACK_OPENAI_API_KEY="sk-role-fallback",
            ),
            "analysis_fallback",
        )
        self.assertEqual(captured["api_key"], "sk-role-fallback")

    def test_redacted_marker_never_reaches_client(self):
        _, captured = self._run_default_probe_capture(
            _hermetic_env(ANALYSIS_OPENAI_API_KEY="sk-role-analysis"),
            "analysis",
        )
        self.assertNotEqual(captured["api_key"], "***")
        self.assertNotIn("***", str(captured["api_key"]))

    def test_unresolvable_key_fails_closed(self):
        env = _hermetic_env()
        with patch.dict(os.environ, env), patch(
            "tradingagents.llm_clients.roles.get_llm_api_key", return_value="",
        ):
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr._default_llm_probe(
                    role="analysis", provider="openai", model="gpt-fake",
                    backend_url=None, api_key=REDACTED, max_retries=0,
                )
        self.assertIn("no API key", str(ctx.exception))


class PerRoleProbeTest(_PreflightTest):
    """C: same provider/model/endpoint with different role keys probes twice."""

    def test_same_combo_different_role_keys_probes_each_role(self):
        probe = _RecordingProbe()
        cfg = _valid_cfg(
            analysis_model="gpt-fake-shared",
            decision_model="gpt-fake-shared",
        )
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys()):
            lr.run_preflight(cfg, runtime, self._deps(probe))
        roles = [c["role"] for c in probe.calls]
        self.assertIn("analysis", roles)
        self.assertIn("decision", roles)
        self.assertEqual(roles.count("analysis"), 1)
        self.assertEqual(roles.count("decision"), 1)

    def test_screening_is_probed_independently(self):
        probe = _RecordingProbe()
        cfg = _valid_cfg(
            analysis_model="gpt-fake-shared",
            decision_model="gpt-fake-shared",
            screening_model="gpt-fake-shared",
        )
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys()):
            lr.run_preflight(cfg, runtime, self._deps(probe))
        roles = [c["role"] for c in probe.calls]
        self.assertEqual(roles.count("screening"), 1)
        screening = next(c for c in probe.calls if c["role"] == "screening")
        self.assertEqual(screening["api_key"], "***")


class FallbackPreflightTest(_PreflightTest):
    """D: fallback is an extra independent probe route."""

    def _cfg_with_fallback(self, **overrides):
        return _valid_cfg(
            analysis_fallback_provider="openai",
            analysis_fallback_model="gpt-fake-fallback",
            **overrides,
        )

    def test_configured_fallback_gets_own_probe(self):
        probe = _RecordingProbe()
        cfg = self._cfg_with_fallback()
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys()):
            lr.run_preflight(cfg, runtime, self._deps(probe))
        roles = [c["role"] for c in probe.calls]
        self.assertIn("analysis_fallback", roles)
        self.assertEqual(roles.count("analysis_fallback"), 1)
        fb_call = next(c for c in probe.calls if c["role"] == "analysis_fallback")
        self.assertEqual(fb_call["api_key"], "***")

    def test_unconfigured_fallback_probes_nothing_extra(self):
        probe = _RecordingProbe()
        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys()):
            lr.run_preflight(cfg, runtime, self._deps(probe))
        roles = [c["role"] for c in probe.calls]
        self.assertNotIn("analysis_fallback", roles)
        self.assertEqual(len(roles), 3)

    def test_invalid_fallback_credential_fails_preflight(self):
        probe = _RecordingProbe(
            exc=ProviderFailure(
                role="analysis_fallback", provider="openai",
                model="gpt-fake-fallback", attempts=1,
                category="permanent", detail="401 unauthorized",
            ),
            fail_role="analysis_fallback",
        )
        cfg = self._cfg_with_fallback()
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _all_role_keys()):
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.run_preflight(cfg, runtime, self._deps(probe))
        self.assertIn("llm_probe:analysis_fallback", str(ctx.exception))
        # The three primary routes were probed before the fallback failed.
        self.assertEqual(
            [c["role"] for c in probe.calls],
            ["analysis", "decision", "screening", "analysis_fallback"],
        )

    def test_missing_fallback_credential_fails_closed(self):
        probe = _RecordingProbe()
        cfg = self._cfg_with_fallback()
        runtime = lr.build_runtime_config(cfg)
        # Primary routes have role-specific keys; the fallback resolves
        # neither ANALYSIS_FALLBACK_* nor (patched) the provider standard key.
        env = _all_role_keys()
        env.pop("ANALYSIS_FALLBACK_OPENAI_API_KEY")
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env), patch(
            "tradingagents.llm_clients.roles.get_llm_api_key", return_value="",
        ):
            with self.assertRaises(lr.LongRunStop) as ctx:
                lr.run_preflight(cfg, runtime, self._deps(probe))
        self.assertIn("no API key for probe role=analysis_fallback",
                      str(ctx.exception))
        # The three primary routes probed; the fallback probe never ran.
        self.assertEqual(
            [c["role"] for c in probe.calls],
            ["analysis", "decision", "screening"],
        )


class FallbackPersistenceTest(_PreflightTest):
    """E: save/load/runtime/resolve full chain for the fallback keys."""

    def test_fallback_save_load_roundtrip_preserved(self):
        cfg = _valid_cfg(
            analysis_fallback_provider="deepseek",
            analysis_fallback_model="deepseek-chat",
            analysis_fallback_backend_url="https://api.deepseek.com",
        )
        lr.save_long_run_config(cfg)
        loaded = lr.load_long_run_config()
        self.assertEqual(loaded["analysis_fallback_provider"], "deepseek")
        self.assertEqual(loaded["analysis_fallback_model"], "deepseek-chat")
        self.assertEqual(
            loaded["analysis_fallback_backend_url"], "https://api.deepseek.com",
        )

    def test_build_runtime_config_preserves_fallback_keys(self):
        cfg = _valid_cfg(
            analysis_fallback_provider="deepseek",
            analysis_fallback_model="deepseek-chat",
        )
        runtime = lr.build_runtime_config(cfg)
        self.assertEqual(runtime["analysis_fallback_provider"], "deepseek")
        self.assertEqual(runtime["analysis_fallback_model"], "deepseek-chat")
        self.assertIsNone(runtime["analysis_fallback_backend_url"])

    def test_resolve_role_config_yields_fallback_from_runtime(self):
        from tradingagents.llm_clients.roles import resolve_role_config

        cfg = _valid_cfg(
            analysis_fallback_provider="deepseek",
            analysis_fallback_model="deepseek-chat",
        )
        runtime = lr.build_runtime_config(cfg)
        with patch.dict(os.environ, _hermetic_env()):
            resolved = resolve_role_config(runtime)
        self.assertIsNotNone(resolved.get("analysis_fallback"))
        fb = resolved["analysis_fallback"]
        self.assertEqual(fb.role, "analysis_fallback")
        self.assertEqual(fb.provider, "deepseek")
        self.assertEqual(fb.model, "deepseek-chat")

    def test_all_none_fallback_unchanged_behavior(self):
        from tradingagents.llm_clients.roles import resolve_role_config

        cfg = _valid_cfg()
        runtime = lr.build_runtime_config(cfg)
        self.assertIsNone(runtime["analysis_fallback_provider"])
        self.assertIsNone(runtime["analysis_fallback_model"])
        self.assertIsNone(runtime["analysis_fallback_backend_url"])
        resolved = resolve_role_config(runtime)
        self.assertIsNone(resolved.get("analysis_fallback"))
        self.assertEqual(lr.validate_long_run_config(cfg, runtime), [])

    def test_partial_fallback_pair_fails_closed(self):
        from tradingagents.llm_clients.roles import (
            RoleConfigError,
            resolve_role_config,
        )

        cfg = _valid_cfg(analysis_fallback_provider="deepseek")
        runtime = lr.build_runtime_config(cfg)
        with self.assertRaises(RoleConfigError):
            resolve_role_config(runtime)

    def test_saved_partial_fallback_survives_load_and_still_fails_closed(self):
        from tradingagents.llm_clients.roles import (
            RoleConfigError,
            resolve_role_config,
        )

        cfg = _valid_cfg(analysis_fallback_model="deepseek-chat")
        lr.save_long_run_config(cfg)
        loaded = lr.load_long_run_config()
        self.assertEqual(loaded["analysis_fallback_model"], "deepseek-chat")
        runtime = lr.build_runtime_config(loaded)
        with self.assertRaises(RoleConfigError):
            resolve_role_config(runtime)


class CliFallbackSetupTest(unittest.TestCase):
    """F: interactive fallback setup — decline, enable, preserve, hygiene."""

    def _run_collect(self, existing, answers):
        import cli.main as cli_main

        outputs = iter(answers)
        confirms = []
        prompts = []
        secrets_prompted = []

        def fake_confirm(prompt, default=False, **kwargs):
            confirms.append(str(prompt))
            value = next(outputs)
            return default if value == "default" else bool(value)

        def fake_prompt(text="", default="", **kwargs):
            prompts.append(str(text))
            value = next(outputs)
            return "" if value == "default" else value

        def fake_secret(label):
            prompts.append(f"secret:{label}")
            secrets_prompted.append(label)
            return f"sk-{label.lower()}-fresh"

        env = _hermetic_env(
            OPENAI_API_KEY="sk-openai-standard",
            ALPACA_API_KEY="PK-test",
            ALPACA_SECRET_KEY="sk-alpaca-test",
        )
        # patch.dict overlays; force the fallback provider's standard key to
        # be absent so the missing-credential prompt path is exercised.
        env["DEEPSEEK_API_KEY"] = ""
        from tradingagents.dataflows import config as cfgmod

        with patch.dict(os.environ, env), patch(
            "typer.confirm", side_effect=fake_confirm,
        ), patch("typer.prompt", side_effect=fake_prompt), patch(
            "cli.main._prompt_secret", side_effect=fake_secret,
        ), patch("sys.stdin", SimpleNamespace(isatty=lambda: True)), patch.dict(
            cfgmod._runtime_api_keys, {}, clear=True,
        ):
            cfg, secrets = cli_main.collect_long_run_config(dict(existing))
        return cfg, secrets, confirms, prompts

    def _base_existing(self):
        return _valid_cfg()

    def test_decline_fallback_keeps_keys_none(self):
        cfg, secrets, confirms, prompts = self._run_collect(
            self._base_existing(), [False],
        )
        self.assertIsNone(cfg["analysis_fallback_provider"])
        self.assertIsNone(cfg["analysis_fallback_model"])
        self.assertIsNone(cfg["analysis_fallback_backend_url"])
        fallback_confirms = [c for c in confirms if "fallback" in c.lower()]
        self.assertEqual(len(fallback_confirms), 1)
        self.assertFalse(any("fallback" in p.lower() for p in prompts))
        self.assertNotIn("ANALYSIS_FALLBACK_OPENAI_API_KEY", secrets)

    def test_enable_fallback_collects_provider_model_url_and_credential(self):
        cfg, secrets, _, prompts = self._run_collect(
            self._base_existing(),
            [True, "deepseek", "deepseek-chat", "https://api.deepseek.com"],
        )
        self.assertEqual(cfg["analysis_fallback_provider"], "deepseek")
        self.assertEqual(cfg["analysis_fallback_model"], "deepseek-chat")
        self.assertEqual(
            cfg["analysis_fallback_backend_url"], "https://api.deepseek.com",
        )
        # Fallback provider has no usable key -> one secret prompt; the
        # secret lands in the secrets dict (destined for .env), never in cfg.
        self.assertIn("DEEPSEEK_API_KEY", secrets)
        self.assertEqual(secrets["DEEPSEEK_API_KEY"], "sk-deepseek_api_key-fresh")
        self.assertNotIn("sk-deepseek_api_key-fresh", str(cfg))

    def test_existing_complete_fallback_preserved_without_reask(self):
        existing = _valid_cfg(
            analysis_fallback_provider="deepseek",
            analysis_fallback_model="deepseek-chat",
            analysis_fallback_backend_url="https://api.deepseek.com",
        )
        cfg, secrets, confirms, prompts = self._run_collect(existing, [])
        fallback_confirms = [c for c in confirms if "fallback" in c.lower()]
        fallback_prompts = [p for p in prompts if "fallback" in p.lower()]
        self.assertEqual(fallback_confirms, [])
        self.assertEqual(fallback_prompts, [])
        self.assertEqual(cfg["analysis_fallback_provider"], "deepseek")
        self.assertEqual(cfg["analysis_fallback_model"], "deepseek-chat")
        self.assertEqual(
            cfg["analysis_fallback_backend_url"], "https://api.deepseek.com",
        )
        self.assertNotIn("sk-", str(cfg))

    def test_fallback_summary_line_is_secret_free(self):
        cfg = _valid_cfg(
            analysis_fallback_provider="deepseek",
            analysis_fallback_model="deepseek-chat",
            analysis_fallback_backend_url="https://user:pass@relay.example/v1?token=x",
        )
        endpoint = lr.sanitize_url(cfg.get("analysis_fallback_backend_url"))
        summary = (
            f"analysis_fallback: {cfg['analysis_fallback_provider']}/"
            f"{cfg['analysis_fallback_model']} endpoint={endpoint or 'provider default'}"
        )
        self.assertEqual(
            summary,
            "analysis_fallback: deepseek/deepseek-chat "
            "endpoint=https://relay.example/v1",
        )
        self.assertNotIn("user:pass", summary)
        self.assertNotIn("token", summary)


class NeutralUserAgentTest(unittest.TestCase):
    """G: neutral UA on custom endpoints, SDK defaults on official ones."""

    def _ua(self, headers):
        if not headers:
            return None
        return headers.get("User-Agent")

    def test_native_openai_custom_base_url_gets_neutral_ua(self):
        model = GPT5ChatModel(
            model="gpt-5-mini", api_key="sk-test", base_url="https://router.example/v1",
        )
        self.assertEqual(
            self._ua(model._client.default_headers), _NEUTRAL_USER_AGENT,
        )
        self.assertEqual(model._client.max_retries, 0)

    def test_native_openai_official_endpoint_untouched(self):
        model = GPT5ChatModel(model="gpt-5-mini", api_key="sk-test")
        ua = self._ua(model._client.default_headers)
        self.assertNotEqual(ua, _NEUTRAL_USER_AGENT)

    def test_chat_openai_custom_base_url_gets_neutral_ua(self):
        llm = get_chat_model(
            "gpt-4.1", api_key="sk-test", base_url="https://router.example/v1",
        )
        self.assertEqual(self._ua(llm.default_headers), _NEUTRAL_USER_AGENT)
        self.assertEqual(llm.max_retries, 0)

    def test_chat_openai_official_endpoint_untouched(self):
        llm = get_chat_model("gpt-4.1", api_key="sk-test")
        ua = self._ua(llm.default_headers)
        self.assertNotEqual(ua, _NEUTRAL_USER_AGENT)

    def test_generic_openai_compatible_custom_route_gets_neutral_ua(self):
        from tradingagents.llm_clients.openai_client import OpenAIClient

        llm = OpenAIClient(
            "deepseek-chat", None, provider="deepseek", api_key="sk-test",
        ).get_llm()
        self.assertEqual(self._ua(llm.default_headers), _NEUTRAL_USER_AGENT)

    def test_generic_openai_compatible_official_openai_untouched(self):
        from tradingagents.llm_clients.openai_client import OpenAIClient

        llm = OpenAIClient(
            "gpt-4.1", None, provider="openai", api_key="sk-test",
            model_role="quick",
        ).get_llm()
        ua = self._ua(llm.default_headers)
        self.assertNotEqual(ua, _NEUTRAL_USER_AGENT)

    def test_caller_user_agent_not_clobbered(self):
        from tradingagents.agents.utils.gpt5_llm import _endpoint_headers

        headers = _endpoint_headers(
            "https://router.example/v1", {"User-Agent": "my-agent/2.0"},
        )
        self.assertEqual(headers["User-Agent"], "my-agent/2.0")

    def test_generic_path_caller_default_headers_survive(self):
        # Full integration path: OpenAIClient kwargs -> llm_kwargs ->
        # _endpoint_default_headers -> ChatOpenAI. A caller-supplied
        # default_headers (including an explicit User-Agent) must reach the
        # constructed LLM untouched instead of being dropped before the
        # neutral-UA decision.
        from tradingagents.llm_clients.openai_client import OpenAIClient

        llm = OpenAIClient(
            "deepseek-chat",
            provider="deepseek",
            api_key="sk-test",
            default_headers={
                "User-Agent": "my-agent/2.0",
                "X-Test-Header": "keep-me",
            },
        ).get_llm()
        self.assertEqual(llm.default_headers["User-Agent"], "my-agent/2.0")
        self.assertEqual(llm.default_headers["X-Test-Header"], "keep-me")

    def test_max_retries_zero_preserved(self):
        from tradingagents.llm_clients.openai_client import OpenAIClient

        llm = OpenAIClient(
            "deepseek-chat", "https://relay.example/v1", provider="deepseek",
            api_key="sk-test",
        ).get_llm()
        self.assertEqual(llm.max_retries, 0)


class PhaseBEnvIsolationTest(unittest.TestCase):
    """H: ambient OPENAI_USE_LOCAL / OPENAI_BASE_URL must not flip the
    global-default expectations of the Phase B role tests."""

    def test_role_resolution_tests_ignore_ambient_local_switch(self):
        from tests.test_phase_b_llm_roles import RoleResolutionTests

        with patch.dict(os.environ, {
            "OPENAI_USE_LOCAL": "true",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
        }):
            suite = unittest.TestSuite()
            for name in (
                "test_no_role_keys_keeps_legacy_mode",
                "test_any_role_key_enters_roles_mode_with_defaults",
                "test_global_local_switch_applies_to_defaulted_provider",
            ):
                suite.addTest(RoleResolutionTests(name))
            result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def test_ambient_local_switch_pollutes_unshielded_resolution(self):
        # Sanity check that the env actually matters for a non-hermetic call:
        # a defaulted provider flips to local_openai when the switch is set,
        # which is exactly what the HermeticRoleTest base class shields.
        from tradingagents.llm_clients.roles import resolve_role_config

        config = {
            "llm_provider": "openai",
            "deep_think_llm": "gpt-5.4-mini",
            "analysis_model": "gpt-5.4-mini",
        }
        with patch.dict(os.environ, {
            "OPENAI_USE_LOCAL": "true",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
        }):
            resolved = resolve_role_config(dict(config))
        self.assertEqual(resolved["analysis"].provider, "local_openai")


if __name__ == "__main__":
    unittest.main()
