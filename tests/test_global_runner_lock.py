"""Application-wide runner lock: exactly one Paper runner process.

Covers the outermost global runner lock shared by the unified single
long-run entry and every A/B Paper runner entry (lock ordering: global
runner lock -> mode-specific locks), plus the unified ``long-run`` CLI
dispatch/validation that sits in front of it.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tradingagents.long_run as lr
import tradingagents.long_run_support.state as lr_state


def _patch_app_home(tmp: str):
    return patch.object(lr_state, "app_home", lambda: Path(tmp))


class GlobalRunnerLockTests(unittest.TestCase):
    def test_second_acquisition_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    with self.assertRaises(lr.GlobalRunnerLockBusy):
                        with lr.global_runner_lock():
                            pass

    def test_lock_released_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    pass
                with lr.global_runner_lock():  # re-acquire after release
                    pass

    def test_lock_file_records_holder_and_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock() as path:
                    data = json.loads(Path(path).read_text(encoding="utf-8"))
                    self.assertEqual(data["pid"], os.getpid())
                    self.assertTrue(data["at"])
                self.assertTrue(Path(path).exists())

    def test_global_lock_stacks_outside_phase_d_runner_lock(self):
        # Lock ordering: global runner lock outermost, Phase-D runner_lock
        # innermost; they are distinct files and must not deadlock.
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    with lr.runner_lock():
                        pass

    def test_ab_campaign_main_returns_2_when_lock_held(self):
        from scripts.run_analysis_ab_campaign import main as campaign_main

        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    rc = campaign_main([
                        "--symbol", "AAPL", "--start-date", "2026-06-22",
                        "--results-root", str(Path(tmp) / "ab"),
                    ])
                self.assertEqual(rc, 2)

    def test_ab_legacy_main_returns_2_when_lock_held(self):
        from scripts.run_analysis_ab import main as ab_main

        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    rc = ab_main([
                        "--symbol", "AAPL", "--date", "2026-06-22",
                        "--results-root", str(Path(tmp) / "ab"),
                    ])
                self.assertEqual(rc, 2)

    def test_ab_auto_main_returns_2_when_lock_held(self):
        from scripts.run_analysis_ab_campaign_auto import main as auto_main

        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    rc = auto_main([
                        "--symbol", "AAPL", "--start-date", "2026-06-22",
                        "--results-root", str(Path(tmp) / "ab"),
                    ])
                self.assertEqual(rc, 2)


class UnifiedLongRunEntryTests(unittest.TestCase):
    """CLI validation and dispatch in front of the global runner lock."""

    def _invoke(self, args):
        import cli.main as cli_main
        from typer.testing import CliRunner

        return CliRunner().invoke(cli_main.app, ["long-run", *args])

    def test_invalid_mode_rejected(self):
        result = self._invoke(["--mode", "bogus"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--mode", result.output)

    def test_invalid_backend_rejected(self):
        result = self._invoke(["--backend", "bogus"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--backend", result.output)

    def test_backend_is_single_mode_only(self):
        result = self._invoke(["--mode", "ab", "--backend", "berkshire"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--backend", result.output)

    def test_non_positive_duration_rejected(self):
        result = self._invoke(["--duration-days", "0"])
        self.assertEqual(result.exit_code, 2)

    def test_ab_options_rejected_in_single_mode(self):
        for args in (
            ["--symbol", "AAPL"],
            ["--start-date", "2026-06-22"],
            ["--resume", "/tmp/x"],
            ["--results-root", "/tmp/x"],
            ["--execute"],
        ):
            result = self._invoke(args)
            self.assertEqual(result.exit_code, 2, args)
            self.assertIn("A/B-mode", result.output)

    def test_single_mode_exits_2_when_global_lock_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_app_home(tmp):
                with lr.global_runner_lock():
                    result = self._invoke(["--mode", "single"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("already active", result.output)

    def test_ab_mode_delegates_to_campaign_launcher(self):
        calls = []

        def _run(command, cwd=None):
            calls.append(list(command))
            return SimpleNamespace(returncode=0)

        with patch("subprocess.run", side_effect=_run):
            result = self._invoke([
                "--mode", "ab", "--symbol", "AAPL",
                "--start-date", "2026-06-22", "--duration-days", "45",
                "--continuous", "--execute", "--paper-notional-usd", "500",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertIn("scripts.run_analysis_ab_campaign_auto", command)
        for expected in ("--symbol", "AAPL", "--start-date", "2026-06-22",
                         "--days", "45", "--continuous", "--execute",
                         "--paper-notional-usd", "500.0"):
            self.assertIn(expected, command)

    def test_ab_mode_propagates_launcher_exit_code(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=2)):
            result = self._invoke([
                "--mode", "ab", "--symbol", "AAPL", "--start-date", "2026-06-22",
            ])
        self.assertEqual(result.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
