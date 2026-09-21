import os
import unittest

from tradingagents.app_identity import (
    APP_HOME,
    DEFAULT_RESULTS_DIR,
    env_name,
    get_env,
    PROJECT_ROOT,
    validate_app_path,
)
from tradingagents.default_config import DEFAULT_CONFIG


class AppIdentityTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "OPENAI_API_KEY",
            "TRADINGBUFFETT_OPENAI_API_KEY",
        ):
            os.environ.pop(key, None)

    def test_environment_names_are_tradingbuffett_only(self):
        self.assertEqual(env_name("OPENAI_API_KEY"), "TRADINGBUFFETT_OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "alpaca-project-secret"
        self.assertIsNone(get_env("OPENAI_API_KEY"))

        os.environ["TRADINGBUFFETT_OPENAI_API_KEY"] = "buffett-project-secret"
        self.assertEqual(get_env("OPENAI_API_KEY"), "buffett-project-secret")

    def test_default_durable_paths_are_not_shared_with_tradingalpaca(self):
        self.assertEqual(APP_HOME.name, ".tradingbuffett")
        self.assertEqual(DEFAULT_RESULTS_DIR.parent, APP_HOME)
        self.assertIn(".tradingbuffett", DEFAULT_CONFIG["memory_log_path"])
        self.assertIn(".tradingbuffett", str(DEFAULT_CONFIG["results_dir"]))
        self.assertIn(".tradingbuffett", DEFAULT_CONFIG["agent_memory_dir"])

    def test_paths_into_sibling_checkout_are_rejected(self):
        sibling = PROJECT_ROOT.parent / "tradingAlpaca" / "results"
        with self.assertRaises(ValueError):
            validate_app_path(sibling, field="results_dir")


if __name__ == "__main__":
    unittest.main()
