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
        from scripts.run_analysis_ab_campaign import _campaign_state_path, finalize_campaign

        sessions = ["2026-06-22", "2026-06-23"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pairs(root, ["2026-06-22"])  # second session missing
            state = _campaign_state(sessions)
            with self.assertRaisesRegex(RuntimeError, "completed pair state"):
                finalize_campaign(root, state, summarize_fn=_summarize_stub(2))
            # Now the integrity passes but the summarizer observes too few.
            _write_pairs(root, sessions)


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

            with self.assertRaisesRegex(RuntimeError, "exactly 2 pairs"):
                finalize_campaign(root, state, summarize_fn=_summarize_stub(1))
            # Exact match finalizes and writes the durable summary.
            summary = finalize_campaign(root, state, summarize_fn=_summarize_stub(2))
            self.assertEqual(summary["campaign"]["campaign_id"], "campaign-test")
            self.assertTrue((root / "campaign_summary.json").exists())
            self.assertTrue((root / "campaign_summary.md").exists())
