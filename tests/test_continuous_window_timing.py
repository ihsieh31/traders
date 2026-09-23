"""Continuous single observation: chunk-extension timing (fake clock).

Production-level tests of ``run_observation_loop`` in continuous mode. The
next chunk — including a trading day exactly ON the boundary — must be
frozen into ``expected_sessions`` BEFORE that session's own target, or the
scheduler only sees it after the close and settles it MISSED without ever
running it. Growth must also never become a hot loop over a weekend, a
holiday, or a calendar outage.

Every test drives the real loop with a fake clock, a fake authoritative
calendar client, and offline graph/execution fakes; all durable long-run
state is written into a TemporaryDirectory.
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytz

import tradingagents.long_run as lr
import tradingagents.long_run_support.state as lr_state

ET = pytz.timezone("US/Eastern")
RUN_TIME_ET = "11:00"
LABOR_DAY_2026 = date(2026, 9, 7)


def _et(year, month, day, hour=0, minute=0):
    return ET.localize(datetime(year, month, day, hour, minute))


def _sessions_between(start, end, holidays=()):
    """Half-open [start, end) weekday sessions minus explicit holidays."""
    holidays = {d.isoformat() if isinstance(d, date) else str(d) for d in holidays}
    out = []
    day = start
    while day < end:
        iso = day.isoformat()
        if day.weekday() < 5 and iso not in holidays:
            out.append(iso)
        day += timedelta(days=1)
    return out


def _ends_et_date(state):
    """Calendar (ET) date of the persisted ends_at boundary."""
    ends = datetime.fromisoformat(state["ends_at"])
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return ends.astimezone(ET).date().isoformat()


class _FakeCalendarClient:
    """Alpaca-shaped calendar: weekdays minus holidays, optional outage.

    ``fail_from`` makes every range starting at/after that date raise, which
    simulates a live calendar outage for one code path at a time.
    """

    def __init__(self, holidays=(), early_closes=None, fail_from=None,
                 empty_from=None):
        self.holidays = {h.isoformat() if isinstance(h, date) else str(h)
                         for h in holidays}
        self.early_closes = dict(early_closes or {})
        self.fail_from = fail_from
        # ``empty_from`` proves an empty range (a valid calendar fact) for
        # ranges starting at/after that date: extension can then append no
        # session at all, which is how the anti-spin fallback is exercised.
        self.empty_from = empty_from
        self.calls = []

    def get_calendar(self, request):
        start = request.start
        end = request.end
        start = start.date() if isinstance(start, datetime) else start
        end = end.date() if isinstance(end, datetime) else end
        self.calls.append((start, end))
        if self.fail_from is not None and start >= self.fail_from:
            raise RuntimeError(f"calendar outage [{start}..{end}]")
        if self.empty_from is not None and start >= self.empty_from:
            return []
        rows = []
        day = start
        while day <= end:
            iso = day.isoformat()
            if day.weekday() < 5 and iso not in self.holidays:
                rows.append({"date": iso, "open": "09:30",
                             "close": self.early_closes.get(iso, "16:00")})
            day += timedelta(days=1)
        return rows


class _SimulationDone(Exception):
    """Raised by the fake clock to end a simulation at a chosen time."""


class _FakeClock:
    """Deterministic clock that lands on declared wake points.

    ``sleep`` advances to the earliest declared wake point (a session target,
    an inspection time, or the stop time) instead of stepping through the
    loop's 60-second idle polls. Wake points therefore must cover every time
    the observation is expected to act; any skipped action shows up as a
    missing/MISSED round journal in the assertions.
    """

    def __init__(self, start, *, stop_at):
        self.now = start
        self.stop_at = stop_at
        self.wake_points = []
        self.observations = []
        self.snapshot = {}
        self.events = []
        self.sleeps = []

    def watch(self, at, label, fn):
        self.observations.append((at, label, fn))

    def wake_at(self, *times):
        self.wake_points.extend(times)

    def now_fn(self):
        return self.now

    def sleep(self, seconds):
        seconds = max(0.0, float(seconds))
        self.sleeps.append(seconds)
        self.events.append(("sleep", self.now))
        requested = self.now + timedelta(seconds=seconds)
        # Jump to the earliest declared wake point (never past one); the stop
        # time is a termination marker, not a wake point, so an idle loop
        # still advances at its own bounded pace.
        candidates = [p for p in self.wake_points if p > self.now]
        candidates += [at for at, _label, _fn in self.observations if at > self.now]
        target = min(candidates) if candidates else requested
        self.now = max(target, self.now + timedelta(seconds=1))
        for entry in sorted(self.observations, key=lambda item: item[0]):
            at, label, fn = entry
            if at <= self.now:
                self.observations.remove(entry)
                self.snapshot[label] = fn()
        if self.now >= self.stop_at:
            raise _SimulationDone(self.now)


class _FakeBroker:
    def __init__(self):
        self._account = SimpleNamespace(
            id="acct-fake", equity=100000.0, cash=50000.0, buying_power=50000.0,
        )

    def get_clock(self):
        return SimpleNamespace(is_open=True, timestamp=datetime.now(timezone.utc))

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return []


class _FakeService:
    def __init__(self):
        self.execute_calls = []

    def enforce_exit_deadlines(self, can_submit=None):
        return {"success": True, "deadline_exits": [], "broker_calls": 0}

    def startup_recover(self, can_submit=None):
        return {"success": True, "account_execution_state": "CLEAN",
                "reconciliation_reasons": []}

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        return {"success": True, "broker_attempted": True, "broker_calls": 1,
                "decision_id": kwargs.get("decision_id")}


class _FakeGraph:
    def __init__(self, config=None):
        self.config = config
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        intent = {"symbol": symbol, "action": "BUY", "target_position": "LONG"}
        return ({"final_trade_intent": intent, "final_trade_decision": "BUY"}, "BUY")


def _plan(symbols, session_date):
    return SimpleNamespace(
        stopped=False,
        deep_analysis_set=list(symbols),
        top20=[{"symbol": s, "rank": i + 1, "screening_score": 90.0 - i,
                "short_reason": "fake"} for i, s in enumerate(symbols)],
        selection_date=session_date, as_of=session_date, cached=False,
        overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
        screening_description="Screening=fake",
        stop_reason_text=lambda: "",
    )


def _config(days, **overrides):
    cfg = lr.default_long_run_config()
    cfg.update({
        "duration_calendar_days": days,
        "continuous": True,
        "run_time_et": RUN_TIME_ET,
        "base_trade_notional_usd": 1000.0,
        "analysts": ["market", "news", "fundamentals"],
        "research_depth": 1,
        "output_language": "English",
        "analysis_provider": "openai", "analysis_model": "gpt-fake-analysis",
        "analysis_backend_url": None,
        "decision_provider": "openai", "decision_model": "gpt-fake-decision",
        "decision_backend_url": None,
        "screening_provider": "openai", "screening_model": "gpt-fake-screening",
        "screening_backend_url": None,
    })
    cfg.update(overrides)
    return cfg


class _LongRunIsolatedTestCase(unittest.TestCase):
    """Durable long-run state lives in a TemporaryDirectory, never in the
    operator's ~/.tradingbuffett (active.json, runs/, journals, events)."""

    def setUp(self):
        import tradingagents.dataflows.config as cfgmod

        from tradingagents.dataflows.market_calendar import clear_calendar_cache
        from tradingagents.safety import SafetyGuard

        self._saved_config = cfgmod.get_config()
        self._cfgmod = cfgmod
        self._tmp = tempfile.TemporaryDirectory(prefix="continuous-")
        root = Path(self._tmp.name)
        self.root = root
        self.long_run_base = root / "long_run"
        self.long_run_base.mkdir(parents=True, exist_ok=True)
        self.results_dir = root / "results"
        clear_calendar_cache()
        self._env = patch.dict(os.environ, {
            "TRADINGBUFFETT_LONG_RUN_DIR": str(self.long_run_base),
            "TRADINGBUFFETT_RESULTS_DIR": str(self.results_dir),
            "TRADINGBUFFETT_CACHE_DIR": str(root / "cache"),
            "TRADINGBUFFETT_EXECUTION_DB": str(root / "execution.sqlite3"),
            "TRADINGBUFFETT_EXECUTION_LOCK_DIR": str(root / "execution_locks"),
        })
        self._env.start()
        self._guard = SafetyGuard(
            state_path=root / "safety" / "state.json",
            kill_switch_path=root / "safety" / "KILL_SWITCH",
        )
        self._patches = [
            patch.object(lr, "base_dir", lambda: self.long_run_base),
            patch.object(lr_state, "base_dir", lambda: self.long_run_base),
            patch("tradingagents.safety.get_safety_guard",
                  return_value=self._guard),
            patch("tradingagents.regime.regime_risk_multiplier",
                  return_value=1.0),
            patch("tradingagents.portfolio.adjust_new_position_notional",
                  side_effect=lambda s, a, amount, gather_state=None, config=None: amount),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        from tradingagents.dataflows.market_calendar import clear_calendar_cache

        for p in reversed(self._patches):
            p.stop()
        clear_calendar_cache()
        self._env.stop()
        self._cfgmod._config = dict(self._saved_config)
        self._tmp.cleanup()

    # --- helpers -----------------------------------------------------

    def _state(self, *, started_et, days, sessions):
        cfg = _config(days)
        state = lr.new_observation_state(
            cfg, expected_sessions=list(sessions),
            now=started_et.astimezone(timezone.utc),
        )
        lr.save_active_state(state)
        return cfg, state

    def _runtime(self, cfg):
        runtime = lr.build_runtime_config(cfg)
        runtime["results_dir"] = str(self.results_dir)
        return runtime

    def _seed_completed(self, run_id, session_date):
        journal = lr.new_round_journal(session_date, ["AAA"])
        journal["status"] = "COMPLETED"
        journal["finished_at"] = lr.utc_now_iso()
        lr.save_round_journal(run_id, journal)

    def _deps(self, clock, calendar, symbols=("AAA",)):
        self.service = _FakeService()
        self.graphs = []

        def factory(config):
            graph = _FakeGraph(config)
            self.graphs.append(graph)
            return graph

        def screening_fn(config, refresh=False):
            return _plan(symbols, clock.now.astimezone(ET).date().isoformat())

        return lr.LongRunDeps(
            screening_fn=screening_fn,
            graph_factory=factory,
            execution_service_factory=lambda: self.service,
            broker_client_factory=lambda: _FakeBroker(),
            alert_fn=lambda subject, body, runtime: {"sent": False},
            sleep_fn=clock.sleep,
            now_fn=clock.now_fn,
            calendar_client=calendar,
        )

    def _run(self, state, cfg, deps):
        """Run the loop until the fake clock reaches its stop time."""
        with lr.runner_lock():
            try:
                return lr.run_observation_loop(state, cfg, self._runtime(cfg), deps)
            except _SimulationDone:
                return None

    def _active(self):
        return lr.load_active_state()

    def _expected(self):
        active = self._active()
        if active is None:
            return None
        return list(active.get("expected_sessions") or [])

    def _ends_et(self):
        active = self._active()
        if active is None:
            return None
        ends = datetime.fromisoformat(active["ends_at"])
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        return ends.astimezone(ET).date().isoformat()

    def _round_file(self, run_id, session_date):
        return self.long_run_base / "runs" / run_id / "rounds" / f"{session_date}.json"

    def _status(self, run_id, session_date):
        path = self._round_file(run_id, session_date)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["status"]

    def _statuses(self, run_id):
        rounds = sorted((self.long_run_base / "runs" / run_id / "rounds").glob("*.json"))
        return {p.stem: json.loads(p.read_text(encoding="utf-8"))["status"]
                for p in rounds}

    def _extension_events(self, run_id):
        path = self.long_run_base / "runs" / run_id / "events.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "continuous_window_extended":
                out.append(record)
        return out

    def _decision_ids(self):
        return [call.get("decision_id") for call in self.service.execute_calls]

    def _counting_extension(self, clock):
        """Record every growth attempt in the same event stream as the sleeps,
        so a hot extension loop (no wait in between) is detectable."""
        real = lr.extend_continuous_window

        def wrapper(*args, **kwargs):
            clock.events.append(("extend", clock.now))
            return real(*args, **kwargs)

        return patch.object(lr, "extend_continuous_window", side_effect=wrapper)

    def assertNoHotGrowth(self, clock):
        kinds = [kind for kind, _at in clock.events]
        self.assertIn("extend", kinds, "the simulation never grew the window")
        for prev, nxt in zip(kinds, kinds[1:]):
            self.assertFalse(
                prev == "extend" and nxt == "extend",
                f"window growth ran back-to-back without waiting: {kinds}",
            )

    def _session_targets(self, session_dates):
        return [_et(int(s[:4]), int(s[5:7]), int(s[8:10]), 11, 0)
                for s in session_dates]


class ShouldExtendProofTests(_LongRunIsolatedTestCase):
    """The growth proof: only a spent chunk authorizes extension."""

    def _state(self, ends_at, expected, *, continuous=True):
        return {
            "run_id": "run-proof",
            "continuous": continuous,
            "expected_sessions": list(expected),
            "ends_at": ends_at.isoformat(),
        }

    def _should(self, state, ends_at, now, **kwargs):
        if "calendar_client" not in kwargs and "calendar_rows" not in kwargs:
            chunk_sessions = [
                session for session in state.get("expected_sessions", [])
                if session < ends_at.astimezone(ET).date().isoformat()
            ]
            if chunk_sessions:
                # Keep this proof test hermetic: the target is 11:00 ET on a
                # regular session, so supply the calendar row it needs rather
                # than resolving a live Alpaca client during an offline run.
                kwargs["calendar_rows"] = [
                    {"date": chunk_sessions[-1], "close": "16:00"}
                ]
        return lr.should_extend_continuous_window(
            state=state, ends_at=ends_at, now=now,
            run_time_et=RUN_TIME_ET, **kwargs)

    def test_non_continuous_never_grows(self):
        ends = _et(2026, 8, 10, 20)
        state = self._state(ends, ["2026-08-07"], continuous=False)
        self.assertFalse(self._should(state, ends, _et(2026, 8, 10, 21)))

    def test_grows_only_after_the_chunk_last_session_target(self):
        ends = _et(2026, 8, 10, 20)
        state = self._state(ends, ["2026-08-06", "2026-08-07"])
        # Still before Friday's target: this chunk can still run something.
        self.assertFalse(self._should(state, ends, _et(2026, 8, 7, 10, 59)))
        # Friday's target passed: the chunk is spent, even though the
        # Monday boundary (ends_at) is days away.
        self.assertTrue(self._should(state, ends, _et(2026, 8, 7, 11, 0)))
        self.assertTrue(self._should(state, ends, _et(2026, 8, 10, 10, 0)))

    def test_weekend_does_not_grow_while_a_chunk_session_is_still_ahead(self):
        # Chunk = [8/10, 8/17): sessions 8/10 and 8/14; 8/17 is the next
        # chunk's boundary session, not part of this chunk.
        ends = _et(2026, 8, 17, 20)
        state = self._state(ends, ["2026-08-10", "2026-08-14"])
        # Friday 09:00, before that day's 11:00 target: the chunk still has
        # an opportunity, so the coming weekend must not grow the window.
        self.assertFalse(self._should(state, ends, _et(2026, 8, 14, 9, 0)))
        # Friday 11:00: the chunk is spent; growing now freezes the next
        # chunk (which owns Monday 8/17) days before its target. Early
        # growth never skips a session — it only adds the following chunk.
        self.assertTrue(self._should(state, ends, _et(2026, 8, 14, 11, 0)))
        self.assertTrue(self._should(state, ends, _et(2026, 8, 15, 12, 0)))

    def test_chunk_without_sessions_grows(self):
        ends = _et(2026, 8, 10, 20)
        state = self._state(ends, [])
        self.assertTrue(self._should(state, ends, _et(2026, 8, 3, 12, 0)))

    def test_unprovable_target_never_grows(self):
        ends = _et(2026, 8, 10, 20)
        state = self._state(ends, ["2026-08-07"])
        with patch.object(lr, "effective_target_for_session",
                          side_effect=RuntimeError("calendar down")):
            self.assertFalse(self._should(state, ends, _et(2026, 8, 10, 10, 0)))


class BoundarySessionTimingTests(_LongRunIsolatedTestCase):
    """Test 1: a trading day exactly ON the boundary is frozen — and run —
    BEFORE its target, never settled MISSED post-close."""

    def test_boundary_session_frozen_before_its_target_and_not_missed(self):
        # Sunday 2026-08-02 20:00 ET + 8 calendar days -> ends Monday
        # 2026-08-10 20:00 ET; 8/10 is itself a session (next chunk).
        cfg, state = self._state(
            started_et=_et(2026, 8, 2, 20), days=8,
            sessions=_sessions_between(date(2026, 8, 2), date(2026, 8, 10)))
        run_id = state["run_id"]
        self.assertEqual(_ends_et_date(state), "2026-08-10")
        for session_date in state["expected_sessions"]:
            self._seed_completed(run_id, session_date)

        calendar = _FakeCalendarClient()
        clock = _FakeClock(_et(2026, 8, 10, 10, 55), stop_at=_et(2026, 8, 10, 11, 30))
        clock.wake_at(_et(2026, 8, 10, 11, 0))
        clock.watch(_et(2026, 8, 10, 10, 59), "expected_before_target",
                    lambda: list(self._expected()))
        clock.watch(_et(2026, 8, 10, 10, 59), "ends_before_target",
                    self._ends_et)

        self._run(state, cfg, self._deps(clock, calendar))

        # Frozen strictly before the boundary session's own 11:00 target.
        self.assertIn("2026-08-10", clock.snapshot["expected_before_target"])
        self.assertEqual(clock.snapshot["ends_before_target"], "2026-08-18")
        # It really ran (once): COMPLETED, never MISSED.
        self.assertEqual(self._status(run_id, "2026-08-10"), "COMPLETED")
        self.assertNotIn("MISSED", self._statuses(run_id).values())
        self.assertEqual(
            self._decision_ids(), [f"{run_id}-2026-08-10-AAA"])
        # Same observation identity; the window grew, nothing was restarted.
        active = self._active()
        self.assertEqual(active["run_id"], run_id)
        self.assertTrue(active["continuous"])
        self.assertEqual(len(self._extension_events(run_id)), 1)

    def test_boundary_session_is_never_missed_by_the_close_sweep(self):
        cfg, state = self._state(
            started_et=_et(2026, 8, 2, 20), days=8,
            sessions=_sessions_between(date(2026, 8, 2), date(2026, 8, 10)))
        run_id = state["run_id"]
        for session_date in state["expected_sessions"]:
            self._seed_completed(run_id, session_date)

        calendar = _FakeCalendarClient()
        clock = _FakeClock(_et(2026, 8, 10, 10, 55), stop_at=_et(2026, 8, 10, 11, 30))
        clock.wake_at(_et(2026, 8, 10, 11, 0))
        clock.watch(_et(2026, 8, 10, 10, 59), "expected_before_target",
                    lambda: list(self._expected()))
        self._run(state, cfg, self._deps(clock, calendar))

        journal = lr.load_round_journal(run_id, "2026-08-10")
        self.assertEqual(journal["status"], "COMPLETED")
        self.assertNotEqual(journal.get("stop_reason"), "MISSED_PROCESS_DOWN")
        self.assertIn("2026-08-10", clock.snapshot["expected_before_target"])


class LiveContinuousObservationTests(_LongRunIsolatedTestCase):
    """Test 2: the process stays online from creation to the boundary
    session — no restart, no duplicate execution, no MISSED."""

    def _run_live(self):
        cfg, state = self._state(
            started_et=_et(2026, 8, 2, 20), days=8,
            sessions=_sessions_between(date(2026, 8, 2), date(2026, 8, 10)))
        run_id = state["run_id"]
        calendar = _FakeCalendarClient()
        clock = _FakeClock(_et(2026, 8, 2, 20), stop_at=_et(2026, 8, 10, 11, 30))
        clock.wake_at(*self._session_targets(state["expected_sessions"]))
        clock.wake_at(_et(2026, 8, 10, 11, 0))
        clock.watch(_et(2026, 8, 10, 8, 0), "boundary_morning", lambda: {
            "expected": list(self._expected()), "ends": self._ends_et()})
        deps = self._deps(clock, calendar)
        with self._counting_extension(clock):
            self._run(state, cfg, deps)
        return cfg, state, run_id, clock

    def test_boundary_session_is_frozen_over_the_weekend_and_runs_once(self):
        _cfg, state, run_id, clock = self._run_live()

        # Frozen long before the boundary Monday's 11:00 target.
        observed = clock.snapshot["boundary_morning"]
        self.assertIn("2026-08-10", observed["expected"])
        self.assertEqual(observed["ends"], "2026-08-18")

        # Same observation identity, still continuous.
        active = self._active()
        self.assertIsNotNone(active)
        self.assertEqual(active["run_id"], state["run_id"])
        self.assertEqual(active["run_id"], run_id)
        self.assertTrue(active["continuous"])

        # Every session of both chunks ran exactly once; none was missed.
        statuses = self._statuses(run_id)
        ran = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
               "2026-08-07", "2026-08-10"]
        self.assertEqual(statuses, {s: "COMPLETED" for s in ran})
        decision_ids = self._decision_ids()
        self.assertEqual(sorted(decision_ids),
                         sorted(f"{run_id}-{s}-AAA" for s in ran))
        self.assertEqual(len(decision_ids), len(set(decision_ids)))

        # The window grew once (after Friday's session) and covers the next
        # chunk without gaps, duplicates or a re-run on the boundary date.
        self.assertEqual(len(self._extension_events(run_id)), 1)
        self.assertEqual(
            self._expected(),
            _sessions_between(date(2026, 8, 2), date(2026, 8, 10))
            + _sessions_between(date(2026, 8, 10), date(2026, 8, 18)),
        )
        self.assertNoHotGrowth(clock)

    def test_weekend_alone_never_grows_the_window(self):
        _cfg, state, run_id, clock = self._run_live()
        # Exactly one growth event, and it happened on the Friday that spent
        # the first chunk - not once per idle weekend poll.
        self.assertEqual(len(self._extension_events(run_id)), 1)
        extension_times = [at for kind, at in clock.events if kind == "extend"]
        self.assertEqual(len(extension_times), 1)
        self.assertEqual(extension_times[0].astimezone(ET).date().isoformat(),
                         "2026-08-07")


class MultiChunkContinuousTests(_LongRunIsolatedTestCase):
    """Test 3: three consecutive chunks under one identity."""

    def test_three_chunks_run_ordered_unique_and_without_fake_missed(self):
        cfg, state = self._state(
            started_et=_et(2026, 8, 2, 20), days=8,
            sessions=_sessions_between(date(2026, 8, 2), date(2026, 8, 10)))
        run_id = state["run_id"]
        # Simulated span: chunk 1 (8/3-8/7), chunk 2 (8/10-8/17), chunk 3
        # (8/18-8/25) plus the first session of chunk 4 (8/26).
        sessions_in_span = _sessions_between(date(2026, 8, 3), date(2026, 8, 27))
        calendar = _FakeCalendarClient()
        clock = _FakeClock(_et(2026, 8, 2, 20), stop_at=_et(2026, 8, 26, 11, 30))
        clock.wake_at(*self._session_targets(sessions_in_span))
        deps = self._deps(clock, calendar)
        with self._counting_extension(clock):
            self._run(state, cfg, deps)

        active = self._active()
        self.assertEqual(active["run_id"], run_id)
        self.assertTrue(active["continuous"])

        # Ordered, unique, contiguous: every weekday from the start up to the
        # newest chunk boundary (2026-09-03), with no gap and no repeat.
        expected = self._expected()
        self.assertEqual(expected, sorted(expected))
        self.assertEqual(len(expected), len(set(expected)))
        self.assertEqual(expected, _sessions_between(date(2026, 8, 3), date(2026, 9, 3)))

        # Every session that came due ran exactly once; none is MISSED.
        statuses = self._statuses(run_id)
        self.assertNotIn("MISSED", statuses.values())
        self.assertEqual(sorted(statuses), sessions_in_span)
        self.assertEqual(set(statuses.values()), {"COMPLETED"})
        decision_ids = self._decision_ids()
        self.assertEqual(len(decision_ids), len(sessions_in_span))
        self.assertEqual(len(set(decision_ids)), len(sessions_in_span))

        # Three chunk growths for three spent chunks - not one per poll.
        self.assertEqual(len(self._extension_events(run_id)), 3)
        self.assertNoHotGrowth(clock)


class HolidayAndOutageTests(_LongRunIsolatedTestCase):
    """Test 4: weekends, holidays and calendar outages must never turn into
    runaway growth or a lost session."""

    def _holiday_state(self):
        # Sunday 2026-08-30 20:00 ET + 8 days -> boundary Monday 2026-09-07
        # 20:00 ET, which is Labor Day (a market holiday). The next session
        # is Tuesday 2026-09-08.
        return self._state(
            started_et=_et(2026, 8, 30, 20), days=8,
            sessions=_sessions_between(date(2026, 8, 30), date(2026, 9, 7),
                                      holidays=[LABOR_DAY_2026]))

    def test_long_weekend_freezes_the_next_session_and_never_runs_away(self):
        cfg, state = self._holiday_state()
        run_id = state["run_id"]
        first_chunk = list(state["expected_sessions"])
        self.assertEqual(first_chunk, _sessions_between(date(2026, 8, 31), date(2026, 9, 5)))
        self.assertEqual(_ends_et_date(state), "2026-09-07")

        calendar = _FakeCalendarClient(holidays=[LABOR_DAY_2026])
        clock = _FakeClock(_et(2026, 8, 30, 20), stop_at=_et(2026, 9, 8, 11, 30))
        clock.wake_at(*self._session_targets(first_chunk))
        clock.wake_at(_et(2026, 9, 8, 11, 0))
        clock.watch(_et(2026, 9, 8, 8, 0), "after_holiday", lambda: {
            "expected": list(self._expected()), "ends": self._ends_et()})
        deps = self._deps(clock, calendar)
        with self._counting_extension(clock):
            self._run(state, cfg, deps)

        observed = clock.snapshot["after_holiday"]
        self.assertIn("2026-09-08", observed["expected"])
        self.assertEqual(observed["ends"], "2026-09-15")

        # The holiday itself is never scheduled and never recorded at all.
        self.assertNotIn("2026-09-07", self._expected())
        self.assertIsNone(self._status(run_id, "2026-09-07"))
        self.assertFalse(self._round_file(run_id, "2026-09-07").exists())

        # One growth for the spent chunk over a four-day market break; the
        # next session still ran exactly once and nothing was missed.
        statuses = self._statuses(run_id)
        self.assertEqual(statuses,
                         {**{s: "COMPLETED" for s in first_chunk},
                          "2026-09-08": "COMPLETED"})
        self.assertNotIn("MISSED", statuses.values())
        self.assertEqual(len(self._extension_events(run_id)), 1)
        self.assertEqual(
            self._decision_ids(),
            [f"{run_id}-{s}-AAA" for s in first_chunk + ["2026-09-08"]],
        )
        self.assertNoHotGrowth(clock)
        self.assertEqual(self._active()["run_id"], run_id)

    def test_extension_that_appends_no_session_waits_instead_of_spinning(self):
        cfg, state = self._holiday_state()
        run_id = state["run_id"]
        first_chunk = list(state["expected_sessions"])
        # The chunk already ran (all sessions terminal), so the scheduler has
        # nothing left in this chunk and the sweep has nothing to settle.
        for session_date in first_chunk:
            self._seed_completed(run_id, session_date)
        # Every range starting on/after 2026-09-05 proves empty, so the growth
        # attempt after the spent chunk appends no session at all: the loop
        # must keep its bounded wait instead of trying again immediately.
        calendar = _FakeCalendarClient(
            holidays=[LABOR_DAY_2026], empty_from=date(2026, 9, 5))
        clock = _FakeClock(_et(2026, 9, 4, 11, 5), stop_at=_et(2026, 9, 4, 12, 0))
        deps = self._deps(clock, calendar)
        with self._counting_extension(clock):
            self._run(state, cfg, deps)

        kinds = [kind for kind, _at in clock.events]
        self.assertEqual(kinds.count("extend"), 1, kinds)
        # It then waited out the whole simulated span at the loop's own pace.
        self.assertGreaterEqual(len(clock.sleeps), 50)
        self.assertLessEqual(max(clock.sleeps), 60.0)
        self.assertNoHotGrowth(clock)
        self.assertEqual(self._statuses(run_id),
                         {s: "COMPLETED" for s in first_chunk})
        self.assertEqual(self._decision_ids(), [])
        self.assertIsNotNone(self._active())

    def test_repeated_empty_growth_never_spins(self):
        """The fall-through guard itself: a spent chunk whose growth keeps
        appending nothing must sleep between attempts, never loop hot."""
        cfg, state = self._holiday_state()
        run_id = state["run_id"]
        first_chunk = list(state["expected_sessions"])
        for session_date in first_chunk:
            self._seed_completed(run_id, session_date)
        real_ends = datetime.fromisoformat(state["ends_at"])

        def _empty_growth(state_arg, long_cfg, deps):
            """Record the attempt, advance the boundary, append no session."""
            clock.events.append(("extend", clock.now))
            ends = datetime.fromisoformat(state_arg["ends_at"])
            state_arg["ends_at"] = (ends + timedelta(days=8)).isoformat()
            return state_arg

        clock = _FakeClock(_et(2026, 9, 4, 11, 5), stop_at=_et(2026, 9, 4, 11, 25))
        deps = self._deps(clock, _FakeCalendarClient(holidays=[LABOR_DAY_2026]))
        with patch.object(lr, "next_due_session", return_value=None),                 patch.object(lr, "extend_continuous_window",
                             side_effect=_empty_growth):
            self._run(state, cfg, deps)

        kinds = [kind for kind, _at in clock.events]
        attempts = kinds.count("extend")
        self.assertGreaterEqual(attempts, 5, kinds)
        self.assertGreaterEqual(len(clock.sleeps), attempts)
        self.assertLessEqual(max(clock.sleeps), 60.0)
        self.assertNoHotGrowth(clock)
        # Twenty simulated minutes of empty growths: the boundary advanced
        # once per attempt, never in a burst.
        grown = datetime.fromisoformat(state["ends_at"]) - real_ends
        self.assertEqual(grown, timedelta(days=8 * attempts))
        self.assertEqual(self._statuses(run_id),
                         {s: "COMPLETED" for s in first_chunk})
        self.assertIsNotNone(self._active())

    def test_calendar_outage_during_extension_fails_closed(self):
        cfg, state = self._holiday_state()
        run_id = state["run_id"]
        # The scheduler's own lookups (day -/+ 14 windows) still resolve;
        # only the growth query for the next chunk is unavailable.
        calendar = _FakeCalendarClient(
            holidays=[LABOR_DAY_2026], fail_from=date(2026, 9, 5))
        clock = _FakeClock(_et(2026, 9, 4, 11, 5), stop_at=_et(2026, 9, 4, 12, 0))
        deps = self._deps(clock, calendar)
        result = self._run(state, cfg, deps)

        self.assertIsNotNone(result)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(state["stop"]["code"], "CALENDAR_UNAVAILABLE")
        payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["stop"]["code"], "CALENDAR_UNAVAILABLE")
        self.assertTrue(Path(result["md_path"]).is_file())
        # Fail-closed: the window is closed, never left RUNNING unreported.
        self.assertIsNone(self._active())


if __name__ == "__main__":
    unittest.main()
