import fcntl
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tradingagents.default_config import DEFAULT_CONFIG
from scripts.run_analysis_ab_campaign import _campaign_lock, run_campaign


ET = ZoneInfo("America/New_York")


def _now(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(11), tzinfo=ET)


def _sessions(start: date, end: date, _client, *, date):
    # Offline calendar fixture: includes a Saturday start, skips the 2026
    # Independence Day observed holiday, and has enough future NYSE sessions.
    holidays = {"2026-07-03"}
    result = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor.isoformat() not in holidays:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _summary(_root):
    return {
        "backends": {
            "traders": {"completed": 1, "failed": 0, "invalid": 0, "llm_calls": 2,
                        "total_tokens": 30, "latency_seconds": 1.5, "orders": 1,
                        "deduped": 0, "holds": 0},
            "berkshire": {"completed": 1, "failed": 0, "invalid": 0, "llm_calls": 2,
                          "total_tokens": 30, "latency_seconds": 1.5, "orders": 1,
                          "deduped": 0, "holds": 0},
        }
    }


def _completed_pair(**_kwargs):
    return {"status": "COMPLETED"}


def _equity_snapshot(traders, berkshire, *, captured_at):
    return {
        "traders": {"account": "A", "account_ref": "traders-ref", "equity": traders,
                    "captured_at": captured_at},
        "berkshire": {"account": "B", "account_ref": "berkshire-ref", "equity": berkshire,
                      "captured_at": captured_at},
    }


def _preflight_snapshot(traders, berkshire):
    return {
        "traders": {"account": "A", "account_ref": "traders-ref", "equity": traders,
                    "market_date": "2026-06-22"},
        "berkshire": {"account": "B", "account_ref": "berkshire-ref", "equity": berkshire,
                      "market_date": "2026-06-22"},
    }


class AnalysisABCampaignTests(unittest.TestCase):
    def _run(self, root, *, start="2026-06-20", days=2, now=None, **kwargs):
        pair_runner = kwargs.pop("pair_runner", _completed_pair)
        summarize_fn = kwargs.pop("summarize_fn", _summary)
        return run_campaign(
            symbol="AAPL",
            start_date=start,
            days=days,
            results_root=root,
            base_config=DEFAULT_CONFIG,
            now=now or _now("2026-06-22"),
            calendar_client=object(),
            session_fetcher=_sessions,
            pair_runner=pair_runner,
            summarize_fn=summarize_fn,
            **kwargs,
        )

    def test_create_campaign_freezes_requested_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp) / "campaign", now=_now("2026-06-19"))
            state = result["state"]
            self.assertEqual(result["outcome"], "not_due")
            self.assertEqual(state["status"], "RUNNING")
            self.assertEqual(state["target_days"], 2)
            self.assertEqual(state["sessions"], ["2026-06-22", "2026-06-23"])
            self.assertEqual(state["completed_dates"], [])

    def test_saturday_start_uses_next_nyse_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp) / "campaign", start="2026-06-20", now=_now("2026-06-19"))
            self.assertEqual(result["state"]["sessions"][0], "2026-06-22")

    def test_holiday_is_not_counted_as_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                Path(tmp) / "campaign", start="2026-07-02", days=2, now=_now("2026-07-01")
            )
            self.assertEqual(result["state"]["sessions"], ["2026-07-02", "2026-07-06"])

    def test_completed_pair_marks_only_today_complete(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs["trade_date"])
            return _completed_pair()

        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp) / "campaign", pair_runner=runner)
            self.assertEqual(result["outcome"], "session_completed")
            self.assertEqual(calls, ["2026-06-22"])
            self.assertEqual(result["state"]["completed_dates"], ["2026-06-22"])

    def test_partial_pair_stops_before_next_date(self):
        calls = []

        def partial(**kwargs):
            calls.append(kwargs["trade_date"])
            return {"status": "PARTIAL"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            first = self._run(root, pair_runner=partial)
            self.assertEqual(first["outcome"], "pair_unfinished")
            self.assertEqual(first["state"]["status"], "STOPPED")
            second = run_campaign(
                resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-23"),
                calendar_client=object(), session_fetcher=_sessions, pair_runner=_completed_pair,
                summarize_fn=_summary,
            )
            self.assertEqual(second["outcome"], "previous_session_unfinished")
            self.assertEqual(calls, ["2026-06-22"])

    def test_crash_restart_retries_same_partial_date(self):
        calls = []

        def partial(**kwargs):
            calls.append(kwargs["trade_date"])
            return {"status": "PARTIAL"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            self._run(root, pair_runner=partial)
            resumed = run_campaign(
                resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-22"),
                calendar_client=object(), session_fetcher=_sessions,
                pair_runner=lambda **kwargs: calls.append(kwargs["trade_date"]) or _completed_pair(),
                summarize_fn=_summary,
            )
            self.assertEqual(resumed["outcome"], "session_completed")
            self.assertEqual(calls, ["2026-06-22", "2026-06-22"])
            self.assertEqual(resumed["state"]["completed_dates"], ["2026-06-22"])

    def test_completed_dates_are_not_rerun_after_resume(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs["trade_date"])
            return _completed_pair()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            self._run(root, pair_runner=runner)
            resumed = run_campaign(
                resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-23"),
                calendar_client=object(), session_fetcher=_sessions, pair_runner=runner,
                summarize_fn=_summary,
            )
            self.assertEqual(resumed["outcome"], "completed")
            self.assertEqual(calls, ["2026-06-22", "2026-06-23"])

    def test_config_and_environment_route_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            self._run(root, now=_now("2026-06-19"))
            changed = dict(DEFAULT_CONFIG, quick_think_llm="changed-model")
            with self.assertRaisesRegex(RuntimeError, "configuration or resolved LLM route changed"):
                run_campaign(
                    resume=root, base_config=changed, now=_now("2026-06-19"),
                    calendar_client=object(), session_fetcher=_sessions, pair_runner=_completed_pair,
                    summarize_fn=_summary,
                )
            with patch.dict("os.environ", {"TRADINGBUFFETT_OPENAI_BASE_URL": "https://example.invalid/v1"}):
                with self.assertRaisesRegex(RuntimeError, "configuration or resolved LLM route changed"):
                    run_campaign(
                        resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-19"),
                        calendar_client=object(), session_fetcher=_sessions, pair_runner=_completed_pair,
                        summarize_fn=_summary,
                    )

    def test_thirty_sessions_complete_without_a_thirty_first_pair(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs["trade_date"])
            return _completed_pair()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            first = self._run(root, days=30, pair_runner=runner)
            sessions = first["state"]["sessions"]
            result = first
            for day in sessions[1:]:
                result = run_campaign(
                    resume=root, base_config=DEFAULT_CONFIG, now=_now(day),
                    calendar_client=object(), session_fetcher=_sessions, pair_runner=runner,
                    summarize_fn=_summary,
                )
            self.assertEqual(result["outcome"], "completed")
            self.assertEqual(result["state"]["status"], "COMPLETED")
            self.assertEqual(calls, sessions)

    def test_final_broker_equity_is_authoritative_and_accounts_stay_mapped(self):
        ending = _equity_snapshot(103000.0, 99000.0, captured_at="end")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       return_value=_preflight_snapshot(100000.0, 100000.0)):
                result = self._run(
                    root, days=1, execute_paper=True, paper_notional_usd=500.0,
                    broker_snapshotter=lambda: ending,
                )
            performance = result["summary"]["performance"]
            self.assertEqual(performance["traders"]["pnl"], 3000.0)
            self.assertEqual(performance["traders"]["return_pct"], 3.0)
            self.assertEqual(performance["traders"]["account_ref"], "traders-ref")
            self.assertEqual(performance["berkshire"]["account_ref"], "berkshire-ref")

    def test_completed_finalizer_restart_regenerates_summary_without_pair(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            self._run(root, days=1, pair_runner=lambda **kwargs: calls.append(1) or _completed_pair())
            (root / "campaign_summary.md").unlink()
            result = run_campaign(
                resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-23"),
                calendar_client=object(), session_fetcher=_sessions,
                pair_runner=lambda **kwargs: self.fail("finalizer must not rerun a pair"),
                summarize_fn=_summary,
            )
            self.assertEqual(result["outcome"], "completed")
            self.assertEqual(calls, [1])
            self.assertTrue((root / "campaign_summary.md").exists())

    def test_paper_starting_equity_is_not_captured_before_first_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                Path(tmp) / "campaign", now=_now("2026-06-19"), execute_paper=True,
                paper_notional_usd=500.0,
                broker_snapshotter=lambda: self.fail("future campaign must not read broker equity"),
            )
            self.assertEqual(result["outcome"], "not_due")
            self.assertIsNone(result["state"]["starting_equity"])

    def test_paper_starting_equity_is_persisted_before_first_pair(self):
        events = []

        def runner(**kwargs):
            events.append("pair")
            state = json.loads((Path(kwargs["results_root"]) / "campaign_state.json").read_text())
            self.assertEqual(state["starting_equity"]["traders"]["equity"], 100000.0)
            return {"status": "PARTIAL"}

        with tempfile.TemporaryDirectory() as tmp:
            def preflight(**_kwargs):
                events.append("preflight")
                return _preflight_snapshot(100000.0, 100000.0)

            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight", side_effect=preflight):
                result = self._run(
                    Path(tmp) / "campaign", execute_paper=True, paper_notional_usd=500.0,
                    pair_runner=runner,
                )
            self.assertEqual(result["outcome"], "pair_unfinished")
            self.assertEqual(events, ["preflight", "pair"])

    def test_initial_preflight_market_closed_keeps_baseline_null(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       side_effect=RuntimeError("Alpaca Paper market is closed")):
                result = self._run(
                    Path(tmp) / "campaign", execute_paper=True, paper_notional_usd=500.0,
                    pair_runner=lambda **_kwargs: calls.append("pair") or _completed_pair(),
                )
            self.assertEqual(result["outcome"], "market_closed")
            self.assertIsNone(result["state"]["starting_equity"])
            self.assertEqual(calls, [])

    def test_invalid_initial_preflight_keeps_baseline_null_and_fails_closed(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       side_effect=RuntimeError("Paper account A must start flat")):
                with self.assertRaisesRegex(RuntimeError, "must start flat"):
                    self._run(
                        root, execute_paper=True, paper_notional_usd=500.0,
                        pair_runner=lambda **_kwargs: calls.append("pair") or _completed_pair(),
                    )
            state = json.loads((root / "campaign_state.json").read_text())
            self.assertIsNone(state["starting_equity"])
            self.assertEqual(calls, [])

    def test_retry_after_failed_initial_preflight_pins_fresh_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       side_effect=RuntimeError("Paper account A must start flat")):
                with self.assertRaisesRegex(RuntimeError, "must start flat"):
                    self._run(root, execute_paper=True, paper_notional_usd=500.0)
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       return_value=_preflight_snapshot(120000.0, 120000.0)):
                resumed = run_campaign(
                    resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-22"),
                    calendar_client=object(), session_fetcher=_sessions, pair_runner=_completed_pair,
                    summarize_fn=_summary,
                )
            self.assertEqual(resumed["state"]["starting_equity"]["traders"]["equity"], 120000.0)

    def test_persisted_starting_equity_survives_restart_without_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                           return_value=_preflight_snapshot(100000.0, 100000.0)):
                    self._run(
                        root, execute_paper=True, paper_notional_usd=500.0,
                        pair_runner=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
                    )
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       side_effect=lambda **_kwargs: self.fail("starting preflight must not refresh")):
                resumed = run_campaign(
                    resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-22"),
                    calendar_client=object(), session_fetcher=_sessions, pair_runner=_completed_pair,
                    broker_snapshotter=lambda: self.fail("starting equity must not refresh"),
                    summarize_fn=_summary,
                )
            self.assertEqual(resumed["outcome"], "session_completed")
            self.assertEqual(resumed["state"]["starting_equity"]["traders"]["equity"], 100000.0)

    def test_completed_ending_equity_is_immutable_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                       return_value=_preflight_snapshot(100000.0, 100000.0)):
                completed = self._run(
                    root, days=1, execute_paper=True, paper_notional_usd=500.0,
                    broker_snapshotter=lambda: _equity_snapshot(103000.0, 101500.0, captured_at="end"),
                )
            self.assertEqual(completed["summary"]["performance"]["traders"]["ending_equity"], 103000.0)
            resumed = run_campaign(
                resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-23"),
                calendar_client=object(), session_fetcher=_sessions,
                pair_runner=lambda **_kwargs: self.fail("completed campaign must not rerun pair"),
                broker_snapshotter=lambda: self.fail("completed campaign must not reread broker"),
                summarize_fn=_summary,
            )
            self.assertEqual(resumed["summary"]["performance"]["traders"]["ending_equity"], 103000.0)

    def test_crash_after_ending_snapshot_reuses_persisted_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with self.assertRaisesRegex(RuntimeError, "summary crash"):
                with patch("scripts.run_analysis_ab_campaign._paper_account_preflight",
                           return_value=_preflight_snapshot(100000.0, 100000.0)):
                    self._run(
                        root, days=1, execute_paper=True, paper_notional_usd=500.0,
                        broker_snapshotter=lambda: _equity_snapshot(103000.0, 101500.0, captured_at="end"),
                        summarize_fn=lambda _root: (_ for _ in ()).throw(RuntimeError("summary crash")),
                    )
            state = json.loads((root / "campaign_state.json").read_text())
            self.assertEqual(state["status"], "COMPLETED")
            self.assertEqual(state["ending_equity"]["traders"]["equity"], 103000.0)
            resumed = run_campaign(
                resume=root, base_config=DEFAULT_CONFIG, now=_now("2026-06-23"),
                calendar_client=object(), session_fetcher=_sessions,
                pair_runner=lambda **_kwargs: self.fail("finalizer restart must not rerun pair"),
                broker_snapshotter=lambda: self.fail("finalizer restart must not reread broker"),
                summarize_fn=_summary,
            )
            self.assertEqual(resumed["summary"]["performance"]["traders"]["ending_equity"], 103000.0)

    def test_existing_root_requires_explicit_resume_and_rejects_old_ab_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            self._run(root, now=_now("2026-06-19"))
            with self.assertRaisesRegex(RuntimeError, "use --resume explicitly"):
                self._run(root, start="2026-07-02", now=_now("2026-07-01"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "contaminated"
            root.mkdir()
            (root / "AB_CAMPAIGN.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "existing A/B artifacts"):
                self._run(root, now=_now("2026-06-19"))

    def test_campaign_lock_uses_exclusive_flock_for_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            with patch("fcntl.flock") as flock:
                with _campaign_lock(root):
                    self.assertTrue((root / ".campaign.lock").exists())
            self.assertEqual(flock.call_count, 2)
            self.assertEqual(flock.call_args_list[0].args[1], fcntl.LOCK_EX)
            self.assertEqual(flock.call_args_list[1].args[1], fcntl.LOCK_UN)
