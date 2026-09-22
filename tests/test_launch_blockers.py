"""Launch-blocker regression tests.

Fix 1: terminal replay semantics in the durable executor.
Fix 2: Day-30 final settlement gate before ending-equity pinning.
Fix 3: bounded retry behavior of the thin auto launcher.
Fix 4: final report completeness/integrity gate.

All broker/model/network interaction is mocked; no secrets involved.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from tradingagents.default_config import DEFAULT_CONFIG


def _ready_entry_policy():
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "exit_by": (now + timedelta(days=5)).isoformat(),
            "confirmation": "fixture observed setup"}


def _buy_intent(symbol="AAPL"):
    from tradingagents.agents.schemas import (
        ExecutableAction,
        RiskDecision,
        build_trade_intent_from_risk_decision,
    )

    return build_trade_intent_from_risk_decision(
        symbol=symbol,
        trading_mode="investment",
        current_position="NEUTRAL",
        allow_shorts=False,
        trade_date="2026-01-02",
        decision=RiskDecision(
            action=ExecutableAction.BUY,
            confidence="medium",
            risk_rationale="test setup",
            required_controls="Stop below support.",
            entry_policy=_ready_entry_policy(), stop_loss_price=95.0,
        ),
    ).model_dump(mode="json")


def _mock_broker(status="accepted"):
    broker = MagicMock()
    order = MagicMock()
    order.id = "broker-1"
    order.symbol = "AAPL"
    order.side = "buy"
    order.qty = 5
    order.notional = None
    order.status = status
    order.filled_qty = 5 if status == "filled" else 0
    order.filled_avg_price = 100.0 if status == "filled" else None
    broker_orders = []

    def submit(request):
        order.client_order_id = getattr(request, "client_order_id", None)
        order.updated_at = datetime.now(timezone.utc)
        broker_orders.append(order)
        return order

    broker.submit_order.side_effect = submit
    broker.get_clock.return_value = SimpleNamespace(is_open=True)
    close_order = MagicMock()
    close_order.id = "close-1"
    close_order.symbol = "AAPL"
    close_order.side = "sell"
    close_order.qty = 5
    close_order.status = "accepted"
    broker.close_position.return_value = close_order
    broker.get_account.return_value = SimpleNamespace(
        id="paper-account-1", equity="100000", last_equity="100000",
        cash="100000", buying_power="200000",
    )
    broker.get_all_positions.return_value = []
    broker.get_orders.side_effect = lambda request=None: list(broker_orders)
    return broker


def _fresh_quote(symbol):
    from tradingagents.execution.authority import BrokerQuote

    return BrokerQuote(
        symbol=symbol.replace("/", ""), bid_price=100.0, ask_price=100.1,
        observed_at=datetime.now(timezone.utc),
    )


def _disabled_guard():
    guard = MagicMock()
    guard.enabled = False
    return guard


def _seed_primary(db, decision_id, final_status):
    """Commit a single primary order and drive it to `final_status`."""
    from tradingagents.execution import ExecutionService
    from tradingagents.execution.store import client_order_id_for

    svc = ExecutionService(
        db_path=db, broker_factory=lambda: _mock_broker(), quote_factory=_fresh_quote
    )
    # Bind ownership while the DB is still brand-new (a legacy DB with
    # trading records but no proven owner refuses to bind).
    svc.store.ensure_account_binding("paper-account-1")
    _, orders, _ = svc.store.create_outbox(
        decision_id=decision_id,
        run_id="run-1",
        symbol="AAPL",
        action="BUY",
        target_position="LONG",
        payload_json="{}",
        orders=[
            {
                "client_order_id": client_order_id_for(
                    decision_id, "AAPL", "buy", role="open", seq=0
                ),
                "symbol": "AAPL",
                "side": "buy",
                "quantity": None,
                "notional": 100.0,
            }
        ],
    )
    order_id = orders[0]["order_id"]
    if final_status != "PENDING":
        ok, _ = svc.store.transition_order(order_id, "SUBMITTING")
        assert ok
        if final_status != "SUBMITTING":
            ok, _ = svc.store.transition_order(order_id, final_status)
            assert ok
    return svc


class _ReplayBase(unittest.TestCase):
    def _replay(self, final_status):
        from tradingagents.execution import ExecutionService

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            broker = _mock_broker()
            _seed_primary(db, "dec-replay-1", final_status)
            svc = ExecutionService(
                db_path=db, broker_factory=lambda: broker, quote_factory=_fresh_quote
            )
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                res = svc.execute(
                    trade_intent=_buy_intent(),
                    decision_id="dec-replay-1",
                    dollar_amount=1000,
                )
            return res, broker


class TerminalReplayTests(_ReplayBase):
    def test_rejected_primary_replay_fails_closed_without_posts(self):
        for status in ("REJECTED", "CANCELED", "EXPIRED"):
            with self.subTest(status=status):
                res, broker = self._replay(status)
                self.assertFalse(res["success"])
                self.assertTrue(res.get("deduped"))
                self.assertFalse(res["broker_attempted"])
                self.assertEqual(res["broker_calls"], 0)
                broker.submit_order.assert_not_called()
                self.assertEqual(res["orders"][0]["status"], status)

    def test_unknown_primary_replay_is_not_reported_completed(self):
        res, broker = self._replay("UNKNOWN")
        self.assertFalse(res["success"])
        self.assertEqual(broker.submit_order.call_count, 0)

    def test_accepted_primary_replay_is_not_reported_completed(self):
        # ACCEPTED is a live/uncertain state: the replay must fail closed
        # (either the recovery owner pauses on identity, or the dispatch
        # gate returns deduped failure) and must never POST again.
        res, broker = self._replay("ACCEPTED")
        self.assertFalse(res["success"])
        self.assertEqual(broker.submit_order.call_count, 0)

    def test_filled_primary_replay_reports_success_without_posts(self):
        from tradingagents.execution import ExecutionService

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            broker = _mock_broker(status="filled")
            # Reuse the identical intent object: the completed-replay identity
            # check compares the durable payload, and a regenerated entry
            # policy would carry a fresh expires_at timestamp.
            intent = _buy_intent()
            svc = ExecutionService(
                db_path=db, broker_factory=lambda: broker, quote_factory=_fresh_quote
            )
            svc.store.ensure_account_binding("paper-account-1")
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                first = svc.execute(
                    trade_intent=intent,
                    decision_id="dec-filled-1",
                    dollar_amount=1000,
                )
                second = svc.execute(
                    trade_intent=intent,
                    decision_id="dec-filled-1",
                    dollar_amount=1000,
                )
            self.assertTrue(first["success"])
            self.assertTrue(second["success"])
            self.assertTrue(second.get("deduped"))
            self.assertEqual(broker.submit_order.call_count, 1)




# ---------------------------------------------------------------------------
# Fix 2: Day-30 final settlement gate
# ---------------------------------------------------------------------------

def _seed_order(db_path, decision_id, status, *, symbol="AAPL", side="buy"):
    """Commit a durable primary order and drive it to `status`."""
    from tradingagents.execution.store import ExecutionStore

    store = ExecutionStore(str(db_path))
    # Bind ownership while the DB is brand-new (legacy unbound DBs refuse).
    store.ensure_account_binding("paper-account-1")
    _, orders, _ = store.create_outbox(
        decision_id=decision_id,
        run_id="run-1",
        symbol=symbol,
        action="BUY",
        target_position="LONG",
        payload_json=json.dumps({"decision_id": decision_id}),
        orders=[{
            "client_order_id": f"ci-{decision_id}",
            "symbol": symbol,
            "side": side,
            "quantity": 5,
            "notional": None,
        }],
    )
    if status != "PENDING":
        # Respect the durable state machine: PENDING -> SUBMITTING -> target.
        store.transition_order(orders[0]["order_id"], "SUBMITTING",
                               broker_order_id="broker-seed-1")
        if status != "SUBMITTING":
            store.transition_order(orders[0]["order_id"], status,
                                   filled_qty=5 if status == "FILLED" else None)
    return store, orders[0]


def _campaign_state(sessions):
    return {
        "schema_version": 1,
        "campaign_id": "campaign-test",
        "symbol": "AAPL",
        "start_date": "2026-06-01",
        "target_days": len(sessions),
        "sessions": sessions,
        "completed_dates": list(sessions),
        "current_date": None,
        "status": "RUNNING",
        "execute_paper": False,
        "starting_equity": None,
        "ending_equity": None,
    }


def _write_pairs(root, sessions, symbol="AAPL", status="COMPLETED", symbol_override=None):
    for session in sessions:
        pair_dir = root / session / symbol
        pair_dir.mkdir(parents=True, exist_ok=True)
        (pair_dir / "pair_state.json").write_text(json.dumps({
            "schema_version": 2,
            "pair_id": f"pair-{session}",
            "campaign_fingerprint": "0" * 64,
            "evidence_sha256": "0" * 64,
            "symbol": symbol_override or symbol,
            "trade_date": session,
            "status": status,
        }), encoding="utf-8")


def _summarize_stub(count):
    def _fn(_root, **_kwargs):
        return {"pair_count": count}
    return _fn



class FinalSettlementGateTests(unittest.TestCase):
    def test_unsettled_primary_blocks_ending_equity_pin(self):
        from scripts.run_analysis_ab_campaign import (
            BACKEND_ORDER,
            _campaign_state_path,
            _complete_campaign,
        )

        sessions = ["2026-06-22", "2026-06-23"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = BACKEND_ORDER[0]
            _seed_order(root / "_profiles" / backend / "execution.sqlite3",
                        "dec-unsettled", "ACCEPTED")
            state = _campaign_state(sessions)
            state_path = _campaign_state_path(root)
            broker_snapshotter = unittest.mock.Mock()
            res = _complete_campaign(
                root, state_path, state,
                broker_snapshotter=broker_snapshotter,
                summarize_fn=_summarize_stub(len(sessions)),
            )
            self.assertEqual(res["outcome"], "awaiting_final_settlement")
            self.assertEqual(res["state"]["status"], "RUNNING")
            self.assertIsNone(res["state"].get("ending_equity"))
            self.assertEqual(len(res["unsettled_orders"]), 1)
            self.assertEqual(res["unsettled_orders"][0]["status"], "ACCEPTED")
            broker_snapshotter.assert_not_called()
            self.assertFalse((root / "campaign_summary.json").exists())
            self.assertFalse((root / "final_report.json").exists())
            # state file re-persisted as RUNNING so the next resume re-checks
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "RUNNING")

    def test_filled_primary_completes_once_without_rerunning_pairs(self):
        from scripts.run_analysis_ab_campaign import (
            BACKEND_ORDER,
            _campaign_state_path,
            _complete_campaign,
        )

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = BACKEND_ORDER[0]
            _seed_order(root / "_profiles" / backend / "execution.sqlite3",
                        "dec-filled", "FILLED")
            _write_pairs(root, sessions)
            state = _campaign_state(sessions)
            state_path = _campaign_state_path(root)
            summarize_fn = unittest.mock.Mock(
                side_effect=_summarize_stub(len(sessions)))
            res = _complete_campaign(
                root, state_path, state,
                broker_snapshotter=unittest.mock.Mock(),
                summarize_fn=summarize_fn,
            )
            self.assertEqual(res["outcome"], "completed")
            self.assertEqual(res["state"]["status"], "COMPLETED")
            self.assertTrue((root / "campaign_summary.json").exists())
            summarize_fn.assert_called_once()

    def test_protective_child_never_blocks_finalization(self):
        from types import SimpleNamespace

        from scripts.run_analysis_ab_campaign import (
            BACKEND_ORDER,
            _campaign_state_path,
            _complete_campaign,
            _unsettled_primary_orders,
        )

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = BACKEND_ORDER[0]
            store, parent = _seed_order(
                root / "_profiles" / backend / "execution.sqlite3",
                "dec-prot", "FILLED",
            )
            # A still-live broker-managed protective child (stop leg).
            store.register_protective_child(parent, SimpleNamespace(
                client_order_id="ci-prot-child",
                broker_order_id="broker-child-1",
                symbol="AAPL", side="sell", qty=5,
            ))
            self.assertEqual(_unsettled_primary_orders(root, "AAPL"), [])
            _write_pairs(root, sessions)
            res = _complete_campaign(
                root, _campaign_state_path(root), _campaign_state(sessions),
                broker_snapshotter=unittest.mock.Mock(),
                summarize_fn=_summarize_stub(len(sessions)),
            )
            self.assertEqual(res["outcome"], "completed")

    def test_wrong_symbol_orders_do_not_block(self):
        from scripts.run_analysis_ab_campaign import (
            BACKEND_ORDER,
            _unsettled_primary_orders,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = BACKEND_ORDER[0]
            _seed_order(root / "_profiles" / backend / "execution.sqlite3",
                        "dec-other", "ACCEPTED", symbol="MSFT")
            self.assertEqual(_unsettled_primary_orders(root, "AAPL"), [])




# ---------------------------------------------------------------------------
# Fix 4: final report completeness/integrity gate
# ---------------------------------------------------------------------------

class ReportIntegrityGateTests(unittest.TestCase):
    def _pair_state(self, symbol="AAPL", trade_date="2026-06-22", status="COMPLETED"):
        return {
            "schema_version": 2,
            "pair_id": "pair-x",
            "campaign_fingerprint": "0" * 64,
            "evidence_sha256": "0" * 64,
            "symbol": symbol,
            "trade_date": trade_date,
            "status": status,
        }

    def test_assert_pair_integrity_passes_on_valid_completed_pairs(self):
        from scripts.run_analysis_ab_campaign import _assert_pair_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pairs(root, ["2026-06-22", "2026-06-23"])
            expected = [root / "2026-06-22" / "AAPL", root / "2026-06-23" / "AAPL"]
            state = {"symbol": "AAPL"}
            _assert_pair_integrity(root, state, expected)  # must not raise

    def test_assert_pair_integrity_missing_pair_fails_closed(self):
        from scripts.run_analysis_ab_campaign import _assert_pair_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pairs(root, ["2026-06-22"])
            with self.assertRaisesRegex(RuntimeError, "requires a completed pair"):
                _assert_pair_integrity(
                    root, {"symbol": "AAPL"},
                    [root / "2026-06-22" / "AAPL", root / "2026-06-23" / "AAPL"],
                )

    def test_assert_pair_integrity_rejects_non_completed_status(self):
        from scripts.run_analysis_ab_campaign import _assert_pair_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pairs(root, ["2026-06-22"], status="RUNNING")
            with self.assertRaisesRegex(RuntimeError, "status COMPLETED"):
                _assert_pair_integrity(
                    root, {"symbol": "AAPL"}, [root / "2026-06-22" / "AAPL"])

    def test_assert_pair_integrity_rejects_symbol_mismatch(self):
        from scripts.run_analysis_ab_campaign import _assert_pair_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pairs(root, ["2026-06-22"], symbol_override="MSFT")
            with self.assertRaisesRegex(RuntimeError, "symbol mismatch"):
                _assert_pair_integrity(
                    root, {"symbol": "AAPL"}, [root / "2026-06-22" / "AAPL"])

    def test_assert_pair_integrity_rejects_date_mismatch(self):
        from scripts.run_analysis_ab_campaign import _assert_pair_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pair_dir = root / "2026-06-22" / "AAPL"
            pair_dir.mkdir(parents=True, exist_ok=True)
            (pair_dir / "pair_state.json").write_text(json.dumps(
                self._pair_state(trade_date="2026-06-23")), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "date mismatch"):
                _assert_pair_integrity(root, {"symbol": "AAPL"}, [pair_dir])


    def test_summarize_scoped_excludes_unrelated_pairs(self):
        from scripts.summarize_analysis_ab import summarize

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = ["2026-06-22", "2026-06-23"]
            _write_pairs(root, sessions)
            # An unrelated campaign's pair shares the results root.
            _write_pairs(root, ["2026-07-01"], symbol="MSFT")
            manifest = {
                "schema_version": 2,
                "config_fingerprint": "0" * 64,
                "campaign_id": "campaign-test",
                "symbol": "AAPL",
                "sessions": sessions,
            }
            (root / "AB_CAMPAIGN.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            expected = [root / s / "AAPL" for s in sessions]
            unscoped = summarize(root)
            scoped = summarize(root, expected_pair_dirs=expected)
            self.assertGreater(unscoped["pair_count"], len(sessions))
            self.assertEqual(scoped["pair_count"], len(sessions))

    def test_finalize_campaign_rejects_pair_count_shortfall(self):
        from scripts.run_analysis_ab_campaign import finalize_campaign

        sessions = ["2026-06-22", "2026-06-23"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pairs(root, ["2026-06-22"])  # second session missing
            state = _campaign_state(sessions)
            # Integrity gate fires first: a missing completed pair state.
            with self.assertRaisesRegex(RuntimeError, "completed pair state"):
                finalize_campaign(root, state, summarize_fn=_summarize_stub(2))
            # Now the integrity passes but the summarizer observes too few.
            _write_pairs(root, sessions)
            with self.assertRaisesRegex(RuntimeError, "exactly 2 pairs"):
                finalize_campaign(root, state, summarize_fn=_summarize_stub(1))
            # Exact match finalizes and writes the durable summary.
            summary = finalize_campaign(root, state, summarize_fn=_summarize_stub(2))
            self.assertEqual(summary["campaign"]["campaign_id"], "campaign-test")
            self.assertTrue((root / "campaign_summary.json").exists())
            self.assertTrue((root / "campaign_summary.md").exists())


# ---------------------------------------------------------------------------
# Fix 3: thin auto launcher bounded retry
# ---------------------------------------------------------------------------

class AutoLauncherTests(unittest.TestCase):
    def test_awaiting_settlement_retries_bounded_then_returns(self):
        from scripts.run_analysis_ab_campaign_auto import (
            MAX_SAME_DAY_RETRIES,
            RETRY_DELAY_SECONDS,
            run_auto,
        )

        runner = unittest.mock.Mock(
            return_value={"outcome": "awaiting_final_settlement", "state": {}})
        sleeps: list[float] = []
        res = run_auto(
            symbol="AAPL", start_date="2026-06-01",
            campaign_runner=runner, sleep_fn=sleeps.append,
        )
        self.assertEqual(runner.call_count, MAX_SAME_DAY_RETRIES + 1)
        self.assertEqual(len(sleeps), MAX_SAME_DAY_RETRIES)
        self.assertEqual(set(sleeps), {RETRY_DELAY_SECONDS})
        self.assertEqual(res["outcome"], "awaiting_final_settlement")

    def test_completed_outcome_returns_immediately_without_sleep(self):
        from scripts.run_analysis_ab_campaign_auto import run_auto

        runner = unittest.mock.Mock(
            return_value={"outcome": "completed", "state": {"status": "COMPLETED"}})
        sleeps: list[float] = []
        res = run_auto(
            symbol="AAPL", start_date="2026-06-01",
            campaign_runner=runner, sleep_fn=sleeps.append,
        )
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(res["outcome"], "completed")

    def test_main_rejects_resume_combined_with_symbol(self):
        from scripts.run_analysis_ab_campaign_auto import main

        with self.assertRaises(SystemExit):
            main(["--resume", "/tmp/whatever", "--symbol", "AAPL"])


# ---------------------------------------------------------------------------
# P1-1: auto launcher end-to-end state machine (fake runner, mocked waits)
# ---------------------------------------------------------------------------

class _FakeClock:
    """Sleep advances the clock so bounded-polling waits converge instantly."""

    def __init__(self, start_et):
        from datetime import timezone

        self._t = start_et.astimezone(timezone.utc)

    def now(self):
        return self._t

    def sleep(self, seconds):
        from datetime import timedelta

        self._t = self._t + timedelta(seconds=seconds)

    @property
    def et(self):
        return self._t.astimezone(ZoneInfo("America/New_York"))


def _auto_state(sessions, done=0, execute_paper=False):
    return {
        "status": "RUNNING",
        "symbol": "AAPL",
        "sessions": list(sessions),
        "completed_dates": list(sessions[:done]),
        "execute_paper": execute_paper,
        "current_date": None,
    }


class _AutoRunner:
    """Fake campaign runner producing a realistic outcome sequence."""

    def __init__(self, sessions, script):
        # script maps a 1-based call index to a special outcome; the default
        # advances completed_dates and reports session_completed / completed
        # exactly like the real campaign does.
        self.sessions = list(sessions)
        self.script = dict(script)
        self.calls: list[dict] = []
        self.done = 0

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        special = self.script.get(len(self.calls))
        if special is not None:
            return special(self)
        self.done += 1
        if self.done < len(self.sessions):
            return {"outcome": "session_completed", "state": _auto_state(self.sessions, self.done)}
        return {"outcome": "completed", "state": _auto_state(self.sessions, self.done)}

    @property
    def call_count(self):
        return len(self.calls)


def _patched_auto_targets():
    """Patch only the target-time lookup; waits use an advanced fake clock."""
    return patch(
        "scripts.run_analysis_ab_campaign_auto.effective_target_for_session",
        MagicMock(return_value={"effective_target": "11:00"}),
    )


class AutoStateMechineTests(unittest.TestCase):
    def _run(self, runner, clock, **kwargs):
        from scripts.run_analysis_ab_campaign_auto import run_auto

        # A dummy calendar client keeps the wait paths hermetic; only Test G
        # exercises the lazy campaign-client build (calendar_client=None).
        kwargs.setdefault("calendar_client", object())
        with _patched_auto_targets():
            return run_auto(
                results_root=kwargs.pop("results_root", "/tmp/ab-auto"),
                campaign_runner=runner,
                now_fn=clock.now,
                sleep_fn=clock.sleep,
                **kwargs,
            )

    def test_a_session_completed_continues_to_next_session(self):
        # Test A: session_completed, session_completed, completed -> 3 calls;
        # the launcher must not exit after the first day.
        runner = _AutoRunner(
            ["2026-06-22", "2026-06-23", "2026-06-24"],
            {3: lambda r: {"outcome": "completed", "state": _auto_state(r.sessions, 3)}},
        )
        clock = _FakeClock(datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("America/New_York")))
        res = self._run(runner, clock, symbol="AAPL", start_date="2026-06-22")
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(res["outcome"], "completed")

    def test_b_thirty_sessions_all_run_then_stop(self):
        # Test B: 29 x session_completed + 1 x completed -> exactly 30 calls;
        # no 31st call after the campaign is complete.
        sessions = [f"2026-06-{day:02d}" for day in range(1, 31)]
        runner = _AutoRunner(sessions, {})
        clock = _FakeClock(datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("America/New_York")))
        res = self._run(runner, clock, symbol="AAPL", start_date=sessions[0])
        self.assertEqual(runner.call_count, 30)
        self.assertEqual(res["outcome"], "completed")

    def test_c_not_due_waits_then_resumes_same_root(self):
        # Test C: not_due -> bounded wait until the session target -> resume
        # the same campaign root -> session_completed -> completed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = _AutoRunner(
                ["2026-06-23", "2026-06-24"],
                {
                    1: lambda r: {"outcome": "not_due", "state": _auto_state(r.sessions, 0),
                                  "next_date": "2026-06-23"},
                },
            )
            clock = _FakeClock(datetime(2026, 6, 23, 8, 0, tzinfo=ZoneInfo("America/New_York")))
            res = self._run(runner, clock, results_root=str(root))
            self.assertEqual(res["outcome"], "completed")
            self.assertEqual(runner.call_count, 3)
            self.assertGreaterEqual(clock.et.hour, 11)  # the wait advanced time
            for call in runner.calls[1:]:
                self.assertEqual(call.get("resume"), Path(root))
                self.assertIsNone(call.get("symbol"))
                self.assertIsNone(call.get("start_date"))

    def test_d_market_closed_before_open_uses_schedule_wait_not_retry_budget(self):
        # Test D: market_closed twice before the open must not consume the
        # 3-attempt transient retry budget and must not exit early.
        day = "2026-06-23"

        def market_closed(_r):
            return {"outcome": "market_closed", "state": _auto_state([day], 0),
                    "next_date": day}

        runner = _AutoRunner(
            [day],
            {
                1: market_closed,
                2: market_closed,
                3: lambda r: {"outcome": "session_completed", "state": _auto_state(r.sessions, 1)},
            },
        )
        clock = _FakeClock(datetime(2026, 6, 23, 8, 0, tzinfo=ZoneInfo("America/New_York")))
        res = self._run(runner, clock, symbol="AAPL", start_date=day)
        self.assertEqual(runner.call_count, 4)
        self.assertEqual(res["outcome"], "completed")
        self.assertGreaterEqual(clock.et.hour, 11)  # waited until the target

    def test_e_pair_unfinished_retries_bounded(self):
        # Test E: pair_unfinished must stop after MAX_SAME_DAY_RETRIES.
        from scripts.run_analysis_ab_campaign_auto import MAX_SAME_DAY_RETRIES

        day = "2026-06-23"
        state = _auto_state([day], 0)
        state["current_date"] = day
        calls: list[dict] = []

        def unfinished(**kwargs):
            calls.append(kwargs)
            return {"outcome": "pair_unfinished", "state": dict(state)}

        clock = _FakeClock(datetime(2026, 6, 23, 10, 30, tzinfo=ZoneInfo("America/New_York")))
        res = self._run(unfinished, clock, symbol="AAPL", start_date=day)
        self.assertEqual(len(calls), MAX_SAME_DAY_RETRIES + 1)
        self.assertEqual(res["outcome"], "pair_unfinished")

    def test_f_early_close_effective_target_reuses_existing_helper(self):
        # Test F: the same authority adjusts the target for early closes.
        from datetime import date

        from tradingagents.long_run import effective_target_for_session

        early = date(2026, 12, 24)  # early close 13:00 ET
        info = effective_target_for_session(
            early, "14:00",
            calendar_rows=[{"date": early, "open": "09:30", "close": "13:00"}],
        )
        self.assertEqual(info["effective_target"], "12:30")
        self.assertEqual(info["schedule_adjustment"], "EARLY_CLOSE")
        normal = date(2026, 6, 23)
        info2 = effective_target_for_session(
            normal, "14:00",
            calendar_rows=[{"date": normal, "open": "09:30", "close": "16:00"}],
        )
        self.assertEqual(info2["effective_target"], "14:00")
        self.assertEqual(info2["schedule_adjustment"], "NONE")

    def test_g_paper_campaign_uses_account_a_read_only_calendar_client(self):
        # Test G: with calendar_client=None the launcher builds the campaign
        # Account A read-only calendar client, never the default client.
        import scripts.run_analysis_ab_campaign_auto as auto_mod

        runner = _AutoRunner(
            ["2026-06-22", "2026-06-23"],
            {
                1: lambda r: {"outcome": "session_completed",
                              "state": _auto_state(r.sessions, 1, execute_paper=True)},
                2: lambda r: {"outcome": "completed",
                              "state": _auto_state(r.sessions, 2, execute_paper=True)},
            },
        )
        clock = _FakeClock(datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo("America/New_York")))
        sentinel = object()
        with patch.object(auto_mod, "_campaign_calendar_client",
                          return_value=sentinel) as calendar_factory:
            self._run(runner, clock, symbol="AAPL", start_date="2026-06-22",
                      calendar_client=None)
        calendar_factory.assert_called_once_with(execute_paper=True, supplied=None)


# ---------------------------------------------------------------------------
# P1-2: Day-30 settlement refresh actually reads broker/recovery state
# ---------------------------------------------------------------------------

def _equity_snapshot(captured_at):
    return {
        "traders": {"account": "A", "account_ref": "ref-A",
                    "equity": "100000", "captured_at": captured_at},
        "berkshire": {"account": "B", "account_ref": "ref-B",
                      "equity": "100000", "captured_at": captured_at},
    }


def _paper_state(sessions):
    state = _campaign_state(sessions)
    state["execute_paper"] = True
    state["starting_equity"] = _equity_snapshot("2026-06-22T15:00:00Z")
    return state


class SettlementRefreshTests(unittest.TestCase):
    def test_h_local_accepted_recovers_to_broker_filled_then_completes(self):
        from scripts.run_analysis_ab_campaign import (
            BACKEND_ORDER,
            _campaign_state_path,
            _complete_campaign,
        )

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = BACKEND_ORDER[0]
            store, order = _seed_order(
                root / "_profiles" / backend / "execution.sqlite3",
                "dec-refresh", "ACCEPTED",
            )
            _write_pairs(root, sessions)
            state = _paper_state(sessions)
            refresh_calls: list = []

            def refresh_fn(r, s):
                refresh_calls.append((r, s))
                ok, _ = store.transition_order(order["order_id"], "FILLED", filled_qty=5)
                assert ok

            res = _complete_campaign(
                root, _campaign_state_path(root), state,
                broker_snapshotter=lambda: _equity_snapshot("2026-06-22T20:00:00Z"),
                summarize_fn=_summarize_stub(len(sessions)),
                settlement_refresh_fn=refresh_fn,
            )
            self.assertEqual(res["outcome"], "completed")
            self.assertEqual(len(refresh_calls), 1)
            self.assertIsNotNone(res["state"]["ending_equity"])
            self.assertEqual(res["state"]["status"], "COMPLETED")

    def test_i_still_unsettled_after_refresh_stays_awaiting_without_equity(self):
        from scripts.run_analysis_ab_campaign import (
            BACKEND_ORDER,
            _campaign_state_path,
            _complete_campaign,
        )

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = BACKEND_ORDER[0]
            _seed_order(
                root / "_profiles" / backend / "execution.sqlite3",
                "dec-stuck", "ACCEPTED",
            )
            _write_pairs(root, sessions)
            state = _paper_state(sessions)
            refresh_calls: list = []
            res = _complete_campaign(
                root, _campaign_state_path(root), state,
                broker_snapshotter=unittest.mock.Mock(),
                summarize_fn=_summarize_stub(len(sessions)),
                settlement_refresh_fn=lambda r, s: refresh_calls.append((r, s)),
            )
            self.assertEqual(res["outcome"], "awaiting_final_settlement")
            self.assertEqual(len(refresh_calls), 1)
            self.assertIsNone(res["state"].get("ending_equity"))
            self.assertEqual(res["state"]["status"], "RUNNING")
            self.assertFalse((root / "campaign_summary.json").exists())

    def test_j_refresh_maps_traders_to_account_a_and_berkshire_to_b(self):
        import scripts.run_analysis_ab_campaign as cam

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for backend in ("traders", "berkshire"):
                _seed_order(
                    root / "_profiles" / backend / "execution.sqlite3",
                    f"dec-{backend}", "ACCEPTED",
                )
            with patch(
                "tradingagents.execution.service.ExecutionService"
            ) as service_cls, patch(
                "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client"
            ) as client_factory:
                cam._refresh_broker_settlement(root, _campaign_state(sessions))
            self.assertEqual(service_cls.call_count, 2)
            db_paths = [c.kwargs["db_path"] for c in service_cls.call_args_list]
            self.assertEqual(
                db_paths,
                [root / "_profiles" / "traders" / "execution.sqlite3",
                 root / "_profiles" / "berkshire" / "execution.sqlite3"],
            )
            for call in service_cls.call_args_list:
                call.kwargs["broker_factory"]()
            self.assertEqual(
                [c.kwargs.get("account") for c in client_factory.call_args_list],
                ["A", "B"],
            )
            self.assertTrue(
                all(c.kwargs.get("read_only") is False for c in client_factory.call_args_list)
            )

    def test_k_production_resume_without_injected_refresh_calls_real_refresh(self):
        # No settlement_refresh_fn is passed here: the production entry point
        # (CLI --resume / auto launcher) must still refresh broker state
        # instead of silently skipping recovery.
        import scripts.run_analysis_ab_campaign as cam

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, order = _seed_order(
                root / "_profiles" / "traders" / "execution.sqlite3",
                "dec-default-refresh", "ACCEPTED",
            )
            _write_pairs(root, sessions)
            state = _paper_state(sessions)
            calls: list = []

            def real_refresh(r, s):
                calls.append((r, s))
                ok, _ = store.transition_order(order["order_id"], "FILLED", filled_qty=5)
                assert ok

            with patch.object(cam, "_refresh_broker_settlement", real_refresh):
                res = cam._complete_campaign(
                    root, cam._campaign_state_path(root), state,
                    broker_snapshotter=lambda: _equity_snapshot("2026-06-22T20:00:00Z"),
                    summarize_fn=_summarize_stub(len(sessions)),
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(res["outcome"], "completed")
            self.assertEqual(res["state"]["status"], "COMPLETED")
            self.assertIsNotNone(res["state"]["ending_equity"])

    def test_k2_analysis_only_state_never_touches_broker_refresh(self):
        import scripts.run_analysis_ab_campaign as cam

        sessions = ["2026-06-22"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_order(
                root / "_profiles" / "traders" / "execution.sqlite3",
                "dec-analysis-only", "ACCEPTED",
            )
            _write_pairs(root, sessions)
            state = _campaign_state(sessions)  # execute_paper is not set
            with patch.object(cam, "_refresh_broker_settlement") as refresh:
                res = cam._complete_campaign(
                    root, cam._campaign_state_path(root), state,
                    broker_snapshotter=unittest.mock.Mock(),
                    summarize_fn=_summarize_stub(len(sessions)),
                )
            refresh.assert_not_called()
            self.assertEqual(res["outcome"], "awaiting_final_settlement")
