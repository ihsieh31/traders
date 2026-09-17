"""U14 regression: scheduler worker startup failure must clear running flags.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.4 U14: the Start callback
sets ``analysis_running = True`` before spawning the worker; when the
worker exits early (runtime-config application failure, stale dispatch
token, scheduling failure) without reaching a mode loop, the running flag
stays True and the UI shows a Stop button forever. The worker must clear
its own running/mode flags in a generation-aware finally.
"""

import threading
import unittest
from unittest.mock import patch


class U14WorkerStartupCleanupTests(unittest.TestCase):
    def setUp(self):
        from webui.utils import state as webui_state

        self.app_state = webui_state.app_state
        self._saved = dict(
            run_generation=self.app_state.run_generation,
            analysis_running=self.app_state.analysis_running,
            market_hour_enabled=self.app_state.market_hour_enabled,
            loop_enabled=self.app_state.loop_enabled,
            provider_stop_reason=getattr(self.app_state, "provider_stop_reason", None),
        )

    def tearDown(self):
        self.app_state.run_generation = self._saved["run_generation"]
        self.app_state.analysis_running = self._saved["analysis_running"]
        self.app_state.market_hour_enabled = self._saved["market_hour_enabled"]
        self.app_state.loop_enabled = self._saved["loop_enabled"]
        self.app_state.provider_stop_reason = self._saved["provider_stop_reason"]

    def _scheduler_kwargs(self, generation):
        return dict(
            symbols=["AAPL"],
            market_hour_enabled=False,
            market_hours_list=[],
            loop_enabled=False,
            analysts_market=True,
            analysts_social=False,
            analysts_news=False,
            analysts_fundamentals=False,
            analysts_macro=False,
            research_depth="Shallow",
            allow_shorts=False,
            quick_llm="m",
            deep_llm="m",
            quick_llm_params={},
            deep_llm_params={},
            llm_provider="openai",
            backend_url="",
            output_language="en",
            checkpoint_enabled=False,
            provider_settings={},
            trade_enabled=False,
            trade_amount=1000,
            auto_screening_on=False,
            scheduler_generation=self.app_state.run_generation,
        )

    def test_u14_worker_exception_clears_running_and_mode_flags(self):
        import webui.callbacks.control_callbacks as cc

        self.app_state.analysis_running = True
        self.app_state.loop_enabled = True  # Start already enabled the mode

        def exploding_body():
            raise RuntimeError("worker startup boom")

        with patch.object(cc, "app_state", self.app_state), patch.object(
            cc, "start_analysis", lambda *a, **k: None
        ):
            # Drive the wrapper through an exception by breaking a step the
            # single-run path calls before dispatch: get_next_symbol.
            with patch.object(
                type(self.app_state),
                "get_next_symbol",
                side_effect=RuntimeError("worker startup boom"),
            ):
                thread = threading.Thread(
                    target=cc._scheduler_thread,
                    kwargs=self._scheduler_kwargs(self.app_state.run_generation),
                )
                thread.start()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertFalse(
            self.app_state.analysis_running,
            "U14: worker that died at startup must clear analysis_running",
        )
        self.assertFalse(
            self.app_state.loop_enabled,
            "U14: mode flag must be cleared together with running",
        )

    def test_u14_stale_worker_does_not_clear_new_runs_flags(self):
        import webui.callbacks.control_callbacks as cc

        # Run B already owns the state: its flags must survive an old
        # worker's cleanup.
        self.app_state.analysis_running = True
        self.app_state.loop_enabled = True
        stale_generation = self.app_state.run_generation
        self.app_state.run_generation += 1  # Stop→Start happened

        with patch.object(cc, "app_state", self.app_state):
            cc._scheduler_thread(
                **{**self._scheduler_kwargs(stale_generation),
                   "scheduler_generation": stale_generation}
            )

        self.assertTrue(
            self.app_state.analysis_running,
            "stale worker must not clear the new run's running flag",
        )


if __name__ == "__main__":
    unittest.main()
