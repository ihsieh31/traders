"""Single-mode Berkshire frozen evidence: real packet build, pin/tamper
fail-closed behavior, per-symbol graphs, and continuous chunk boundaries.

These tests exercise the PRODUCTION helper ``_prepare_symbol_graph_config``
and the production round runner ``run_daily_round`` with offline fakes —
not just config dicts — because the original defect was a TypeError at the
actual call site that config-only tests never reached.
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tradingagents.long_run as lr
from tradingagents.experiments.evidence_snapshot import (
    EvidenceIntegrityError,
    evidence_packet_sha256,
)
from tradingagents.long_run_support.symbols import _prepare_symbol_graph_config

SESSION = "2026-06-22"  # Monday, in the past relative to the test host date


class _FakeTool:
    """LangChain-style tool returning deterministic, usable evidence."""

    def __init__(self, name):
        self._name = name

    def invoke(self, args):
        return {"source": self._name, "args": dict(args)}


class _FakeToolkit:
    """Offline toolkit: no live capabilities, every tool returns data."""

    config = {"online_tools": False}

    def __getattr__(self, name):
        if name.startswith("has_"):
            return lambda: False
        return _FakeTool(name)


def _patch_toolkit():
    return patch(
        "tradingagents.experiments.evidence_snapshot.Toolkit",
        lambda config=None: _FakeToolkit(),
    )


def _runtime(tmp, backend="berkshire"):
    return {"analysis_backend": backend, "results_dir": str(Path(tmp) / "results")}


class _TempLongRunStateTestCase(unittest.TestCase):
    """Durable long-run state is confined to a TemporaryDirectory.

    ``active.json``, ``runs/<run_id>/rounds/*.json``, ``manifest.json`` and
    ``events.jsonl`` are all written through ``base_dir()`` / the
    ``LONG_RUN_DIR`` override, so both are patched here: no test in this
    module can create or read files under the operator's
    ``~/.tradingbuffett/long_run``.
    """

    def setUp(self):
        import tradingagents.long_run_support.state as lr_state

        self._lr_state = lr_state
        self._iso_tmp = tempfile.TemporaryDirectory(prefix="long-run-state-")
        root = Path(self._iso_tmp.name)
        self.iso_root = root
        self.long_run_base = root / "long_run"
        self.long_run_base.mkdir(parents=True, exist_ok=True)
        self._iso_env = patch.dict(os.environ, {
            "TRADINGBUFFETT_LONG_RUN_DIR": str(self.long_run_base),
            "TRADINGBUFFETT_RESULTS_DIR": str(root / "results"),
            "TRADINGBUFFETT_CACHE_DIR": str(root / "cache"),
            "TRADINGBUFFETT_EXECUTION_DB": str(root / "execution.sqlite3"),
            "TRADINGBUFFETT_EXECUTION_LOCK_DIR": str(root / "execution_locks"),
        })
        self._iso_env.start()
        self._iso_patches = [
            patch.object(lr, "base_dir", lambda: self.long_run_base),
            patch.object(lr_state, "base_dir", lambda: self.long_run_base),
        ]
        for item in self._iso_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._iso_patches):
            item.stop()
        self._iso_env.stop()
        self._iso_tmp.cleanup()

    def _operator_long_run_files(self):
        """Best-effort snapshot of the real operator long-run state."""
        root = Path.home() / ".tradingbuffett" / "long_run"
        if not root.exists():
            return None
        return sorted(str(f.relative_to(root)) for f in root.rglob("*") if f.is_file())

    def _run_dir(self, run_id):
        return self.long_run_base / "runs" / run_id


class BerkshireFrozenEvidenceHelperTests(_TempLongRunStateTestCase):
    """P1-1: the production helper really builds frozen evidence packets."""

    def test_berkshire_builds_real_packet_and_injects_identity(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_toolkit():
            config, evidence = _prepare_symbol_graph_config(
                {}, _runtime(tmp),
                run_id="run-b", session_date=SESSION, symbol="AAPL",
            )
            self.assertEqual(config["analysis_backend"], "berkshire")
            self.assertEqual(config["analysis_input_mode"], "frozen_evidence")
            packet_path = Path(evidence["path"])
            self.assertTrue(packet_path.exists())
            self.assertEqual(config["evidence_packet_path"], str(packet_path))
            self.assertEqual(config["evidence_packet_sha256"], evidence["sha256"])
            on_disk = json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence_packet_sha256(on_disk), evidence["sha256"])

    def test_distinct_symbols_get_distinct_packets(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_toolkit():
            _, ev_aapl = _prepare_symbol_graph_config(
                {}, _runtime(tmp),
                run_id="run-b", session_date=SESSION, symbol="AAPL",
            )
            _, ev_msft = _prepare_symbol_graph_config(
                {}, _runtime(tmp),
                run_id="run-b", session_date=SESSION, symbol="MSFT",
            )
            self.assertNotEqual(ev_aapl["path"], ev_msft["path"])
            self.assertNotEqual(ev_aapl["sha256"], ev_msft["sha256"])

    def test_traders_helper_is_a_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {"results_dir": str(Path(tmp) / "results")}
            config, evidence = _prepare_symbol_graph_config(
                base, _runtime(tmp, backend="traders"),
                run_id="run-t", session_date=SESSION, symbol="AAPL",
            )
            self.assertIsNone(evidence)
            self.assertNotIn("evidence_packet_path", config)
            self.assertNotIn("analysis_input_mode", config)


class PinnedEvidenceIntegrityTests(_TempLongRunStateTestCase):
    """P2-3: a pinned hash means the packet is authoritative evidence."""

    def _build(self, tmp, symbol="AAPL"):
        with _patch_toolkit():
            return _prepare_symbol_graph_config(
                {}, _runtime(tmp),
                run_id="run-p", session_date=SESSION, symbol=symbol,
            )

    def test_pinned_hash_with_intact_packet_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, evidence = self._build(tmp)
            with _patch_toolkit():
                config, again = _prepare_symbol_graph_config(
                    {}, _runtime(tmp),
                    run_id="run-p", session_date=SESSION, symbol="AAPL",
                    expected_sha256=evidence["sha256"],
                )
            self.assertEqual(again["sha256"], evidence["sha256"])
            self.assertEqual(config["evidence_packet_sha256"], evidence["sha256"])

    def test_pinned_hash_with_tampered_packet_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, evidence = self._build(tmp)
            packet_path = Path(evidence["path"])
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            packet["market"]["tampered"] = {"note": "edited after pinning"}
            packet_path.write_text(json.dumps(packet), encoding="utf-8")
            with _patch_toolkit(), self.assertRaises(EvidenceIntegrityError):
                _prepare_symbol_graph_config(
                    {}, _runtime(tmp),
                    run_id="run-p", session_date=SESSION, symbol="AAPL",
                    expected_sha256=evidence["sha256"],
                )

    def test_pinned_hash_with_missing_packet_fails_without_recapture(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, evidence = self._build(tmp)
            packet_path = Path(evidence["path"])
            packet_path.unlink()
            def _boom(*args, **kwargs):
                raise AssertionError("collector must not run for a pinned packet")
            with patch(
                "tradingagents.experiments.evidence_snapshot._capture_packet",
                side_effect=_boom,
            ) as capture:
                with self.assertRaises(EvidenceIntegrityError):
                    _prepare_symbol_graph_config(
                        {}, _runtime(tmp),
                        run_id="run-p", session_date=SESSION, symbol="AAPL",
                        expected_sha256=evidence["sha256"],
                    )
            capture.assert_not_called()
            self.assertFalse(packet_path.exists(), "packet must not be recreated")

    def test_unpinned_missing_packet_is_captured_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_toolkit():
                _, evidence = _prepare_symbol_graph_config(
                    {}, _runtime(tmp),
                    run_id="run-p", session_date=SESSION, symbol="AAPL",
                    expected_sha256=None,
                )
            self.assertTrue(Path(evidence["path"]).exists())


class _FakeBroker:
    def __init__(self):
        self._account = SimpleNamespace(
            id="acct-fake", equity=100000.0, cash=50000.0, buying_power=50000.0,
        )

    def get_clock(self):
        return SimpleNamespace(is_open=True)

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


class _RecordingGraph:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def propagate(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        return ({"final_trade_intent": None, "final_trade_decision": "HOLD"}, "HOLD")


def _plan(symbols):
    return SimpleNamespace(
        stopped=False,
        deep_analysis_set=list(symbols),
        top20=[{"symbol": s, "rank": i + 1, "screening_score": 90.0 - i,
                "short_reason": "fake"} for i, s in enumerate(symbols)],
        selection_date=SESSION, as_of=SESSION, cached=False,
        overlap_holdings=[], extra_holdings=[], blocked_holdings=[],
        screening_description="Screening=fake",
        stop_reason_text=lambda: "",
    )


class PerSymbolGraphTests(_TempLongRunStateTestCase):
    """Berkshire builds one graph per symbol; Traders reuses one round graph."""

    def setUp(self):
        from tradingagents.safety import SafetyGuard

        super().setUp()
        tmp = self.iso_root
        self.runtime = lr.build_runtime_config(lr.default_long_run_config())
        self.runtime["results_dir"] = str(tmp / "results")
        self._patches = [
            patch("tradingagents.regime.regime_risk_multiplier", return_value=1.0),
            patch("tradingagents.portfolio.adjust_new_position_notional",
                  side_effect=lambda s, a, amount, gather_state=None, config=None: amount),
            patch("tradingagents.safety.get_safety_guard",
                  return_value=SafetyGuard(
                      state_path=tmp / "safety" / "state.json",
                      kill_switch_path=tmp / "safety" / "KILL_SWITCH",
                  )),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        super().tearDown()

    def _run_round(self, backend, symbols=("AAPL", "MSFT")):
        graphs = []

        def factory(config):
            graph = _RecordingGraph(config)
            graphs.append(graph)
            return graph

        service = _FakeService()
        runtime = dict(self.runtime)
        runtime["analysis_backend"] = backend
        deps = lr.LongRunDeps(
            screening_fn=lambda config, refresh=False: _plan(symbols),
            graph_factory=factory,
            execution_service_factory=lambda: service,
            broker_client_factory=lambda: _FakeBroker(),
            sleep_fn=lambda seconds: None,
        )
        journal = lr.run_daily_round(
            run_id=f"run-{backend}", session_date=SESSION,
            long_cfg=lr.default_long_run_config(), runtime=runtime, deps=deps,
        )
        return journal, graphs, service

    def test_berkshire_builds_one_graph_and_packet_per_symbol(self):
        with _patch_toolkit():
            journal, graphs, _service = self._run_round("berkshire")
        self.assertEqual(journal["status"], "COMPLETED")
        self.assertEqual(len(graphs), 2, "Berkshire must build a graph per symbol")
        paths, shas = set(), set()
        for graph in graphs:
            self.assertEqual(graph.config["analysis_input_mode"], "frozen_evidence")
            self.assertEqual(graph.config["analysis_backend"], "berkshire")
            self.assertTrue(Path(graph.config["evidence_packet_path"]).exists())
            paths.add(graph.config["evidence_packet_path"])
            shas.add(graph.config["evidence_packet_sha256"])
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(shas), 2)
        for symbol in ("AAPL", "MSFT"):
            entry = journal["symbols"][symbol]
            self.assertEqual(entry["status"], "DONE")
            self.assertTrue(entry["evidence_packet_sha256"])
            self.assertTrue(Path(entry["evidence_packet_path"]).exists())

    def test_pinned_missing_packet_fails_symbol_closed_on_resume(self):
        """P2-3 at production level: no recapture, no regeneration, no trade."""
        with _patch_toolkit():
            journal, _graphs, _service = self._run_round(
                "berkshire", symbols=("AAPL",))
        entry = journal["symbols"]["AAPL"]
        pinned = entry["evidence_packet_sha256"]
        packet = Path(entry["evidence_packet_path"])
        self.assertTrue(packet.exists())

        # Re-enter the symbol's analysis on resume with the packet removed.
        replay = lr.load_round_journal("run-berkshire", SESSION)
        replay["status"] = "RUNNING"  # the process died before the round ended
        replay["symbols"]["AAPL"]["status"] = "PENDING"
        lr.save_round_journal("run-berkshire", replay)
        packet.unlink()

        def _boom(*args, **kwargs):
            raise AssertionError("pinned packet must never be recaptured")

        with patch("tradingagents.experiments.evidence_snapshot._capture_packet",
                   side_effect=_boom,
        ) as capture:
            journal2, _graphs2, service2 = self._run_round(
                "berkshire", symbols=("AAPL",))
        capture.assert_not_called()
        self.assertEqual(service2.execute_calls, [], "no trade after evidence loss")
        self.assertFalse(packet.exists(), "replacement evidence must not be written")
        failed = journal2["symbols"]["AAPL"]
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("EvidenceIntegrityError",
                      failed["execution_result_summary"]["error"])
        self.assertEqual(failed["trade_intent"], None)
        self.assertEqual(failed["evidence_packet_sha256"], pinned)

    def test_traders_reuses_one_round_level_graph(self):
        journal, graphs, _service = self._run_round("traders")
        self.assertEqual(journal["status"], "COMPLETED")
        self.assertEqual(len(graphs), 1, "Traders shares one graph across symbols")
        self.assertEqual(
            graphs[0].calls, [("AAPL", SESSION), ("MSFT", SESSION)])


    def test_round_artifacts_stay_inside_the_temp_long_run_dir(self):
        """P3: fixed run ids (run-traders / run-berkshire) must never write
        into the operator's real ~/.tradingbuffett/long_run state."""
        operator_before = self._operator_long_run_files()
        journal, _graphs, _service = self._run_round("traders", symbols=("AAPL",))
        run_id = "run-traders"

        self.assertEqual(lr.base_dir(), self.long_run_base)
        self.assertEqual(self._lr_state.base_dir(), self.long_run_base)
        self.assertEqual(lr.run_dir(run_id), self._run_dir(run_id))
        self.assertEqual(journal["session_date"], SESSION)
        self.assertTrue(
            (self._run_dir(run_id) / "rounds" / f"{SESSION}.json").is_file())
        # Nothing appeared in the operator's long-run directory.
        self.assertEqual(self._operator_long_run_files(), operator_before)

    def test_active_state_and_events_stay_inside_the_temp_long_run_dir(self):
        operator_before = self._operator_long_run_files()
        state = lr.new_observation_state(
            lr.default_long_run_config(), expected_sessions=[SESSION])
        lr.save_active_state(state)
        self.assertEqual(lr.active_path(), self.long_run_base / "active.json")
        self.assertTrue((self.long_run_base / "active.json").is_file())
        lr.log_event(state["run_id"], "isolation_probe", {"ok": True})
        self.assertTrue(
            (self._run_dir(state["run_id"]) / "events.jsonl").is_file())
        lr.clear_active_state()
        self.assertFalse((self.long_run_base / "active.json").exists())
        self.assertEqual(self._operator_long_run_files(), operator_before)


class _FakeCalendarClient:
    """Weekday NYSE-style calendar, inclusive [start, end] like Alpaca."""

    def get_calendar(self, request):
        rows = []
        cursor = request.start
        while cursor <= request.end:
            if cursor.weekday() < 5:
                rows.append({"date": cursor.isoformat(),
                             "open": "09:30", "close": "16:00"})
            cursor += timedelta(days=1)
        return rows


class ContinuousChunkBoundaryTests(_TempLongRunStateTestCase):
    """P2-2: chunk extension keeps half-open [start, end) window semantics."""

    def setUp(self):
        from tradingagents.dataflows.market_calendar import clear_calendar_cache

        super().setUp()
        clear_calendar_cache()
        cfg = lr.default_long_run_config()
        cfg["duration_calendar_days"] = 7
        cfg["continuous"] = True
        self.long_cfg = cfg
        # Monday 2026-06-15; chunk boundaries land on Mondays (sessions).
        self.state = lr.new_observation_state(
            cfg,
            expected_sessions=["2026-06-15", "2026-06-16", "2026-06-17",
                               "2026-06-18", "2026-06-19"],
            now=datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc),
        )
        self.deps = lr.LongRunDeps(calendar_client=_FakeCalendarClient())

    def tearDown(self):
        from tradingagents.dataflows.market_calendar import clear_calendar_cache

        clear_calendar_cache()
        super().tearDown()

    def _extend(self):
        return lr.extend_continuous_window(self.state, self.long_cfg, self.deps)

    def _ends_date(self):
        from zoneinfo import ZoneInfo

        ends = datetime.fromisoformat(self.state["ends_at"])
        return ends.astimezone(ZoneInfo("America/New_York")).date().isoformat()

    def test_extension_writes_state_only_under_the_temp_base(self):
        operator_before = self._operator_long_run_files()
        lr.save_active_state(self.state)
        self._extend()
        run_id = self.state["run_id"]
        self.assertEqual(lr.base_dir(), self.long_run_base)
        self.assertTrue((self.long_run_base / "active.json").is_file())
        self.assertTrue((self._run_dir(run_id) / "manifest.json").is_file())
        self.assertTrue((self._run_dir(run_id) / "events.jsonl").is_file())
        self.assertEqual(self._operator_long_run_files(), operator_before)

    def test_boundary_session_not_added_until_next_chunk(self):
        # First extension: new ends_at is Monday 2026-06-29, itself a session.
        self.assertEqual(self._ends_date(), "2026-06-22")
        self._extend()
        self.assertEqual(self._ends_date(), "2026-06-29")
        expected = self.state["expected_sessions"]
        self.assertNotIn("2026-06-29", expected,
                         "boundary session must wait for the next chunk")
        self.assertEqual(expected[-1], "2026-06-26")
        # Scheduler invariant: every expected session is executable, i.e.
        # strictly before ends_at — nothing can age into a fake MISSED.
        self.assertTrue(all(s < self._ends_date() for s in expected))

        # Second extension brings the boundary session in normally.
        self._extend()
        self.assertEqual(self._ends_date(), "2026-07-06")
        expected = self.state["expected_sessions"]
        self.assertIn("2026-06-29", expected)
        self.assertNotIn("2026-07-06", expected)
        self.assertTrue(all(s < self._ends_date() for s in expected))

    def test_multi_chunk_extension_stays_ordered_unique_contiguous(self):
        for _ in range(4):
            self._extend()
        expected = self.state["expected_sessions"]
        self.assertEqual(expected, sorted(expected), "must stay ordered")
        self.assertEqual(len(expected), len(set(expected)), "must stay unique")
        # Contiguous: no weekday gaps inside the covered range.
        cursor = date.fromisoformat(expected[0])
        last = date.fromisoformat(expected[-1])
        weekdays = []
        while cursor <= last:
            if cursor.weekday() < 5:
                weekdays.append(cursor.isoformat())
            cursor += timedelta(days=1)
        self.assertEqual(expected, weekdays)
        self.assertTrue(all(s < self._ends_date() for s in expected))


if __name__ == "__main__":
    unittest.main()
