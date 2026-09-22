"""Single long-run backend selection (traders|berkshire) and A/B isolation.

The single long-run picks ONE analysis backend via ``analysis_backend``;
the A/B campaign always builds both isolated arms.  These tests pin the
plumbing: config defaults/validation, runtime mapping, per-backend artifact
roots, and per-arm A/B config isolation.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tradingagents.long_run as lr
from scripts.run_analysis_ab import BACKEND_ACCOUNT, build_ab_configs
from tradingagents.default_config import DEFAULT_CONFIG


class SingleBackendSelectionTests(unittest.TestCase):
    def test_default_config_is_traders_finite(self):
        cfg = lr.default_long_run_config()
        self.assertEqual(cfg["analysis_backend"], "traders")
        self.assertIs(cfg["continuous"], False)
        self.assertEqual(cfg["duration_calendar_days"], 30)

    def test_validation_accepts_both_backends_and_arbitrary_duration(self):
        for backend in ("traders", "berkshire"):
            cfg = lr.default_long_run_config()
            cfg["analysis_backend"] = backend
            cfg["duration_calendar_days"] = 45  # no 30-day cap anymore
            cfg["continuous"] = True
            errors = lr.validate_long_run_config(cfg)
            self.assertFalse(
                [e for e in errors if "analysis_backend" in e
                 or "duration_calendar_days" in e or "continuous" in e],
                errors,
            )

    def test_validation_rejects_unknown_backend_and_non_bool_continuous(self):
        cfg = lr.default_long_run_config()
        cfg["analysis_backend"] = "prompt-only"
        self.assertTrue(
            any("analysis_backend" in e for e in lr.validate_long_run_config(cfg))
        )
        cfg = lr.default_long_run_config()
        cfg["continuous"] = "yes"
        self.assertTrue(
            any("continuous" in e for e in lr.validate_long_run_config(cfg))
        )
        cfg = lr.default_long_run_config()
        cfg["duration_calendar_days"] = 0
        self.assertTrue(
            any("duration_calendar_days" in e for e in lr.validate_long_run_config(cfg))
        )

    def test_runtime_maps_selected_backend(self):
        for backend in ("traders", "berkshire"):
            cfg = lr.default_long_run_config()
            cfg["analysis_backend"] = backend
            with tempfile.TemporaryDirectory() as tmp:
                with patch("tradingagents.app_identity.app_home",
                           lambda: Path(tmp)):
                    runtime = lr.build_runtime_config(cfg)
            self.assertEqual(runtime["analysis_backend"], backend)
            # Both single backends run the Traders five-analyst topology.
            self.assertEqual(runtime["analysis_profile"], "traders")

    def test_berkshire_runtime_gets_isolated_artifact_roots(self):
        base = lr.default_long_run_config()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tradingagents.app_identity.app_home", lambda: Path(tmp)):
                traders_rt = lr.build_runtime_config(
                    {**base, "analysis_backend": "traders"})
                berkshire_rt = lr.build_runtime_config(
                    {**base, "analysis_backend": "berkshire"})
        # Traders keeps the shared defaults untouched.
        self.assertNotIn("single", str(traders_rt["results_dir"]))
        # Berkshire is remapped under its own root for every path the app
        # consumes (results, cache, execution DBs).
        for key in ("results_dir", "data_cache_dir", "execution_db_path",
                    "recovery_ledger_db_path"):
            self.assertIn(str(Path("single") / "berkshire"), str(berkshire_rt[key]), key)
            self.assertNotEqual(traders_rt.get(key), berkshire_rt[key], key)


class ABBackendIsolationTests(unittest.TestCase):
    def test_ab_arms_are_isolated_and_differ_only_in_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            configs = build_ab_configs(
                DEFAULT_CONFIG,
                symbol="AAPL",
                trade_date="2026-06-22",
                pair_dir=root / "2026-06-22" / "AAPL",
                experiment_root=root,
            )
        traders = configs["traders"]
        berkshire = configs["berkshire"]
        # The formal experiment variable is the analysis backend.
        self.assertEqual(traders["analysis_backend"], "traders")
        self.assertEqual(berkshire["analysis_backend"], "berkshire")
        self.assertEqual(traders["analysis_profile"], berkshire["analysis_profile"])
        # Each arm gets its own Paper account profile and artifact paths.
        self.assertNotEqual(
            traders["_alpaca_account_profile"], berkshire["_alpaca_account_profile"])
        self.assertEqual(
            traders["_alpaca_account_profile"], BACKEND_ACCOUNT["traders"])
        self.assertEqual(
            berkshire["_alpaca_account_profile"], BACKEND_ACCOUNT["berkshire"])
        for key in ("results_dir", "memory_log_path", "agent_memory_dir",
                    "data_cache_dir", "execution_db_path", "long_run_dir",
                    "safety_state_path"):
            self.assertNotEqual(traders[key], berkshire[key], key)
            self.assertIn(str(Path("_profiles") / "traders"), str(traders[key]), key)
            self.assertIn(str(Path("_profiles") / "berkshire"), str(berkshire[key]), key)


if __name__ == "__main__":
    unittest.main()
