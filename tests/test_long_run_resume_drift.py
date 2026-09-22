"""Single long-run resume must not drift from the persisted observation.

An active observation's config is authoritative: resume may restate the
same backend, continuous flag and duration, but a mismatch fails closed
BEFORE any state write (no restart_count bump), broker recovery, or
fresh execution DB/loop initialization. Lifecycle changes are create-time
only.
"""

import unittest
from unittest.mock import patch

import tradingagents.long_run as lr


def _active_state(*, backend="traders", continuous=False, days=30):
    cfg = lr.default_long_run_config()
    cfg["analysis_backend"] = backend
    cfg["continuous"] = continuous
    cfg["duration_calendar_days"] = days
    state = lr.new_observation_state(cfg, expected_sessions=["2026-09-08"])
    lr.save_active_state(state)
    return state


class ResumeDriftTests(unittest.TestCase):
    def _invoke(self, args):
        import cli.main as cli_main
        from typer.testing import CliRunner

        return CliRunner().invoke(cli_main.app, ["long-run", *args])

    def _resume(self, args):
        """Invoke resume with the observation loop stubbed out.

        Returns (result, loop_calls, loops_state_snapshot).
        """
        calls = []

        def _loop(state, long_cfg, runtime, deps=None):
            calls.append((dict(state), dict(long_cfg), dict(runtime)))
            return {"outcome": "completed"}

        with patch.object(lr, "run_observation_loop", side_effect=_loop):
            result = self._invoke(args)
        return result, calls

    def _persisted(self):
        return lr.load_active_state()

    # --- P1-2: backend authority -------------------------------------

    def test_traders_active_refuses_resume_as_berkshire(self):
        _active_state(backend="traders")
        result, calls = self._resume(["--backend", "berkshire"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn(
            "Active observation backend is traders; refusing resume as berkshire.",
            result.output,
        )
        self.assertEqual(calls, [], "no observation loop / recovery / DB setup")
        self.assertEqual(self._persisted()["restart_count"], 0)

    def test_berkshire_active_refuses_resume_as_traders(self):
        _active_state(backend="berkshire")
        result, calls = self._resume(["--backend", "traders"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn(
            "Active observation backend is berkshire; refusing resume as traders.",
            result.output,
        )
        self.assertEqual(calls, [])
        self.assertEqual(self._persisted()["restart_count"], 0)

    def test_same_backend_resume_is_allowed(self):
        _active_state(backend="berkshire")
        result, calls = self._resume(["--backend", "berkshire"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(calls), 1)
        state, long_cfg, _runtime = calls[0]
        self.assertEqual(long_cfg["analysis_backend"], "berkshire")
        self.assertEqual(state["restart_count"], 1)
        self.assertEqual(self._persisted()["restart_count"], 1)

    def test_missing_backend_flag_uses_persisted_backend(self):
        for backend in ("traders", "berkshire"):
            with self.subTest(backend=backend):
                lr.clear_active_state()
                _active_state(backend=backend)
                result, calls = self._resume([])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(calls[0][1]["analysis_backend"], backend)

    # --- P2-4: lifecycle authority (continuous / duration) ------------

    def test_finite_active_refuses_continuous_resume(self):
        _active_state(continuous=False)
        result, calls = self._resume(["--continuous"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("refusing resume with --continuous", result.output)
        self.assertEqual(calls, [])
        self.assertEqual(self._persisted()["restart_count"], 0)
        self.assertFalse(self._persisted()["continuous"])

    def test_continuous_active_resumes_persisted_continuous_without_flag(self):
        _active_state(continuous=True)
        result, calls = self._resume([])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(calls[0][1]["continuous"])
        self.assertTrue(calls[0][0]["continuous"])

    def test_duration_change_on_resume_is_refused(self):
        _active_state(days=30)
        result, calls = self._resume(["--duration-days", "60"])
        self.assertEqual(result.exit_code, 2, result.output)
        # Rich may wrap the message; assert both halves separately.
        self.assertIn("Active observation duration is 30 calendar days", result.output)
        self.assertIn("refusing resume with", result.output)
        self.assertIn("--duration-days 60", result.output)
        self.assertEqual(calls, [])
        persisted = self._persisted()
        self.assertEqual(persisted["restart_count"], 0)
        self.assertEqual(persisted["config"]["duration_calendar_days"], 30)

    def test_same_duration_on_resume_is_allowed(self):
        _active_state(days=30)
        result, calls = self._resume(["--duration-days", "30"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[0][1]["duration_calendar_days"], 30)


if __name__ == "__main__":
    unittest.main()
