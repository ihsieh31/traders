"""Continuous A/B campaign mode: extend the same campaign, never finalize.

Finite campaigns finalize after the frozen window; continuous campaigns
grow the SAME campaign identity (run id, baseline equity, fingerprint,
completed prefix) by one frozen chunk of ``window_chunk_days`` sessions and
return ``session_completed`` so the launcher waits for the next session.
"""

import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.run_analysis_ab_campaign import (
    _extend_campaign_sessions,
    run_campaign,
)
from tradingagents.default_config import DEFAULT_CONFIG

ET = ZoneInfo("America/New_York")


def _now(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(11), tzinfo=ET)


def _sessions(start: date, end: date, _client, *, date):
    result = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _summary(_root):
    return {"pair_count": 2}


def _completed_pair(**kwargs):
    symbol = str(kwargs.get("symbol") or "AAPL").upper()
    trade_date = str(kwargs.get("trade_date") or "")
    root = Path(kwargs.get("results_root"))
    pair_dir = root / trade_date / symbol
    pair_dir.mkdir(parents=True, exist_ok=True)
    (pair_dir / "pair_state.json").write_text(json.dumps({
        "schema_version": 2,
        "pair_id": "fixture-pair",
        "campaign_fingerprint": "0" * 64,
        "evidence_sha256": "0" * 64,
        "symbol": symbol,
        "trade_date": trade_date,
        "status": "COMPLETED",
    }), encoding="utf-8")
    return {"status": "COMPLETED"}


class ContinuousCampaignTests(unittest.TestCase):
    def _create(self, root, *, continuous, days=2, now=None, **kwargs):
        return run_campaign(
            symbol="AAPL",
            start_date="2026-06-20",
            days=days,
            results_root=root,
            base_config=DEFAULT_CONFIG,
            now=now or _now("2026-06-22"),
            calendar_client=object(),
            session_fetcher=_sessions,
            pair_runner=_completed_pair,
            summarize_fn=_summary,
            continuous=continuous,
            **kwargs,
        )

    def _resume(self, root, *, now, **kwargs):
        return run_campaign(
            resume=root,
            base_config=DEFAULT_CONFIG,
            now=now,
            calendar_client=object(),
            session_fetcher=kwargs.pop("session_fetcher", _sessions),
            pair_runner=_completed_pair,
            summarize_fn=_summary,
            **kwargs,
        )

    def test_create_marks_continuous_and_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._create(
                Path(tmp) / "c", continuous=True, now=_now("2026-06-19"))
            state = result["state"]
            self.assertEqual(result["outcome"], "not_due")
            self.assertIs(state["continuous"], True)
            self.assertEqual(state["window_chunk_days"], 2)
            self.assertEqual(state["target_days"], 2)

    def test_finite_default_is_not_continuous(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._create(
                Path(tmp) / "c", continuous=False, now=_now("2026-06-19"))
            self.assertIs(result["state"]["continuous"], False)

    def test_finite_campaign_finalizes_after_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "c"
            first = self._create(root, continuous=False)
            self.assertEqual(first["outcome"], "session_completed")
            second = self._resume(root, now=_now("2026-06-23"))
            self.assertEqual(second["outcome"], "completed")
            self.assertEqual(second["state"]["status"], "COMPLETED")
            self.assertIn("summary", second)

    def test_continuous_extends_same_campaign_instead_of_finalizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "c"
            first = self._create(root, continuous=True)
            self.assertEqual(first["outcome"], "session_completed")
            campaign_id = first["state"]["campaign_id"]
            self.assertEqual(first["state"]["sessions"],
                             ["2026-06-22", "2026-06-23"])

            # Resume WITHOUT passing continuous again: the persisted state
            # flag is the authority.
            second = self._resume(root, now=_now("2026-06-23"))
            self.assertEqual(second["outcome"], "session_completed")
            state = second["state"]
            # Same identity, window grew by exactly one frozen chunk.
            self.assertEqual(state["campaign_id"], campaign_id)
            self.assertEqual(state["sessions"],
                             ["2026-06-22", "2026-06-23",
                              "2026-06-24", "2026-06-25"])
            self.assertEqual(state["target_days"], 4)
            self.assertEqual(state["window_chunk_days"], 2)
            self.assertEqual(state["completed_dates"],
                             ["2026-06-22", "2026-06-23"])
            # Never finalized: still running, no summary, no ending equity.
            self.assertNotEqual(state["status"], "COMPLETED")
            self.assertNotIn("summary", second)
            self.assertIsNone(state["ending_equity"])

            # The campaign keeps going on the newly frozen sessions.
            third = self._resume(root, now=_now("2026-06-24"))
            self.assertEqual(third["outcome"], "session_completed")
            self.assertEqual(third["state"]["campaign_id"], campaign_id)
            self.assertEqual(len(third["state"]["completed_dates"]), 3)

    def test_extension_is_fail_closed_on_calendar_error(self):
        def flaky(start, end, client, *, date):
            if start > date(2026, 6, 23):
                raise RuntimeError("calendar unavailable")
            return _sessions(start, end, client, date=date)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "c"
            self._create(root, continuous=True)
            with self.assertRaisesRegex(RuntimeError, "calendar unavailable"):
                self._resume(root, now=_now("2026-06-23"),
                             session_fetcher=flaky)
            # State on disk was NOT extended: same frozen window, same
            # target; only the honestly completed second session persists.
            state = json.loads(
                (root / "campaign_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sessions"], ["2026-06-22", "2026-06-23"])
            self.assertEqual(state["target_days"], 2)
            self.assertEqual(state["completed_dates"],
                             ["2026-06-22", "2026-06-23"])
            self.assertIs(state["continuous"], True)

    def test_invalid_chunk_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "c"
            self._create(root, continuous=True, now=_now("2026-06-19"))
            state_path = root / "campaign_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["window_chunk_days"] = 0
            before = dict(state)
            with self.assertRaisesRegex(RuntimeError, "window_chunk_days"):
                _extend_campaign_sessions(
                    state, state_path,
                    calendar_client=object(), session_fetcher=_sessions)
            self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
