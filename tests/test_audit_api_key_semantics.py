"""U06/U16 regression tests for API key handling in the WebUI.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4:
- U06: ``load_api_keys`` returns raw .env secret values into browser
  inputs. A server-managed key must be reported as "configured" without
  sending the secret to the client; only keys the operator typed into
  localStorage come back as values.
- U16: Clear used an empty store as the "uninitialized" signal, so the
  next page load re-applied env keys after an explicit Clear, and the
  runtime config kept the old keys. Cleared must be a distinct state
  from uninitialized.
"""

import os
import unittest
from unittest.mock import patch


class _FakeStore:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeApp:
    """Minimal callback registry: maps (output ids) -> (args, fn)."""

    def __init__(self):
        self.callbacks = []

    def callback(self, output, inputs=None, state=None, prevent_initial_call=False):
        def register(fn):
            self.callbacks.append((output, inputs or [], fn))
            return fn
        return register


def _register():
    import webui.callbacks.api_config_callbacks as mod
    app = _FakeApp()
    mod.register_api_config_callbacks(app)
    return mod, app


def _find(app, name):
    for _out, _inp, fn in app.callbacks:
        if fn.__name__ == name:
            return fn
    raise AssertionError(f"callback {name} not registered")


class U06SecretNotReturnedTests(unittest.TestCase):
    def test_u06_env_secret_is_not_returned_to_browser(self):
        mod, app = _register()
        load_fn = _find(app, "load_api_keys")
        os.environ["OPENAI_API_KEY"] = "sk-server-secret-value"

        outputs = load_fn(None)  # no stored keys -> env fallback path
        # 16 api inputs + alpaca-paper + env-file-status
        values = outputs[:16]
        paper, status = outputs[16], outputs[17]

        for api_id, value in zip([c["id"] for c in mod.get_api_configs()], values):
            secret_value = os.getenv(mod.get_api_configs()[0]["env_var"])
            if api_id == "openai":
                self.assertNotIn(
                    "sk-server-secret-value", str(value),
                    "U06: server-managed secret must not be sent to the browser",
                )
        # The status must tell the operator the key is configured.
        self.assertIn("configured", str(status).lower())
        del os.environ["OPENAI_API_KEY"]

    def test_u06_runtime_config_still_receives_env_keys(self):
        from webui.callbacks import api_config_callbacks as real_mod
        applied = {}
        orig = real_mod.apply_api_keys_to_config
        real_mod.apply_api_keys_to_config = lambda keys: applied.update(keys)
        try:
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-server"
            mod, app = _register()
            load_fn = _find(app, "load_api_keys")
            load_fn(None)
            # The pre-mapping dict is keyed by storage id.
            self.assertEqual(applied.get("anthropic"), "sk-ant-server")
        finally:
            real_mod.apply_api_keys_to_config = orig
            os.environ.pop("ANTHROPIC_API_KEY", None)


class U16ClearSemanticsTests(unittest.TestCase):
    def test_u16_cleared_store_marks_explicitly_cleared_not_uninitialized(self):
        mod, app = _register()
        clear_fn = _find(app, "clear_api_keys")
        result = clear_fn(1)
        store_data = result[-1]  # 16 inputs + paper + store
        self.assertEqual(
            store_data.get("_cleared"),
            True,
            "U16: cleared store must carry an explicit cleared marker",
        )
        # All inputs cleared, paper stays True.
        for value in result[:16]:
            self.assertEqual(value, "")
        self.assertTrue(result[16])

    def test_u16_cleared_marker_prevents_env_fallback_on_reload(self):
        mod, app = _register()
        load_fn = _find(app, "load_api_keys")
        clear_fn = _find(app, "clear_api_keys")

        clear_result = clear_fn(1)
        store_data = clear_result[-1]

        os.environ["OPENAI_API_KEY"] = "sk-should-not-come-back"
        try:
            outputs = load_fn(store_data)
            values = outputs[:16]
            self.assertNotIn(
                "sk-should-not-come-back", values,
                "U16: an explicitly cleared store must not fall back to env",
            )
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_u16_uninitialized_store_still_falls_back_to_env(self):
        mod, app = _register()
        load_fn = _find(app, "load_api_keys")
        os.environ["FINNHUB_API_KEY"] = "fh-from-env"
        try:
            # None store = first load (not explicitly cleared): env keys are
            # applied to the runtime config so the run keeps working.
            from webui.callbacks import api_config_callbacks as real_mod
            applied = {}
            orig = real_mod.apply_api_keys_to_config
            real_mod.apply_api_keys_to_config = lambda keys: applied.update(keys)
            try:
                load_fn(None)
            finally:
                real_mod.apply_api_keys_to_config = orig
            self.assertEqual(applied.get("finnhub"), "fh-from-env")
        finally:
            os.environ.pop("FINNHUB_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
