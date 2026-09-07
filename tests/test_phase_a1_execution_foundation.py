"""Phase A.1 foundation tests: paper boundary + durable order ledger.

Covers the 13 acceptance items with temporary SQLite + mocked brokers.
No real Alpaca/model/provider calls.
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tradingagents.agents  # noqa: F401  (production-safe import order)


def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


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


def _mock_broker(order_id="broker-1", status="accepted"):
    broker = MagicMock()
    order = MagicMock()
    order.id = order_id
    order.symbol = "AAPL"
    order.side = "buy"
    order.qty = 5
    order.notional = None
    order.status = status
    broker_orders = []

    def submit(request):
        order.client_order_id = getattr(request, "client_order_id", None)
        order.filled_qty = 0
        order.filled_avg_price = None
        order.updated_at = datetime.now(timezone.utc)
        broker_orders.append(order)
        return order

    broker.submit_order.side_effect = submit
    close_order = MagicMock()
    close_order.id = "close-1"
    close_order.symbol = "AAPL"
    close_order.side = "sell"
    close_order.qty = 5
    close_order.status = "accepted"
    broker.close_position.return_value = close_order
    broker.get_account.return_value = SimpleNamespace(
        id="paper-account-1", equity="100000", last_equity="100000", cash="100000", buying_power="200000"
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


class PaperOnlyLockTests(unittest.TestCase):
    def _factory(self, **env):
        from tradingagents.dataflows import alpaca_utils as au

        return au.get_alpaca_trading_client, au.PaperTradingEnforcementError

    def test_paper_false_fails_closed(self):
        from tradingagents.dataflows import alpaca_utils as au

        with patch.object(
            au, "get_api_key", side_effect=lambda k, e: "k" if "key" in k.lower() or "secret" in k.lower() else "True"
        ):
            with patch.object(au, "get_alpaca_use_paper", return_value="False"):
                with self.assertRaises(au.PaperTradingEnforcementError):
                    au.get_alpaca_trading_client()

    def test_live_and_unknown_base_urls_fail_closed(self):
        from tradingagents.dataflows import alpaca_utils as au

        with patch.object(au, "get_api_key", return_value="dummy"):
            with patch.object(au, "get_alpaca_use_paper", return_value="True"):
                for bad in (
                    "https://api.alpaca.markets",
                    "https://api.alpaca.markets/v2",
                    "https://evil.example.com",
                    "http://localhost:9999",
                ):
                    with self.assertRaises(
                        au.PaperTradingEnforcementError, msg=bad
                    ):
                        au.get_alpaca_trading_client(base_url=bad)

    def test_explicit_paper_url_and_default_are_paper_true(self):
        from tradingagents.dataflows import alpaca_utils as au

        captured = {}
        request_kwargs = []

        class FakeClient:
            def __init__(self, *a, **kw):
                captured.update(kw)
                self._retry = 3
                self._session = SimpleNamespace(request=self._request)

            @staticmethod
            def _request(*args, **kwargs):
                request_kwargs.append(kwargs)

        with patch.object(au, "get_api_key", return_value="dummy"):
            with patch.object(au, "get_alpaca_use_paper", return_value="True"):
                with patch.object(au, "TradingClient", FakeClient):
                    client = au.get_alpaca_trading_client()
                    self.assertTrue(captured.get("paper") is True)
                    self.assertEqual(client._retry, 0)
                    client._session.request("GET", "/v2/account")
                    self.assertEqual(request_kwargs[-1]["timeout"], (3.05, 10.0))
                    captured.clear()
                    au.get_alpaca_trading_client(
                        base_url="https://paper-api.alpaca.markets"
                    )
                    self.assertTrue(captured.get("paper") is True)

    def test_env_base_url_live_fails_closed(self):
        from tradingagents.dataflows import alpaca_utils as au

        with patch.object(au, "get_api_key", return_value="dummy"):
            with patch.object(au, "get_alpaca_use_paper", return_value="True"):
                with patch.dict(
                    os.environ, {"ALPACA_BASE_URL": "https://api.alpaca.markets"}
                ):
                    with self.assertRaises(au.PaperTradingEnforcementError):
                        au.get_alpaca_trading_client()


class StrictRiskBoundaryTests(unittest.TestCase):
    def _risk_state(self):
        return {
            "company_of_interest": "AAPL",
            "risk_debate_state": {
                "history": "",
                "risky_history": "",
                "safe_history": "",
                "neutral_history": "",
                "risky_messages": [],
                "safe_messages": [],
                "neutral_messages": [],
                "current_risky_response": "",
                "current_safe_response": "",
                "current_neutral_response": "",
                "count": 1,
            },
            "trader_investment_plan": "Trader plan requiring confirmed entry and a protective stop",
            "investment_plan": "plan",
            "trade_date": "2026-01-02",
        }

    def _memory(self):
        m = MagicMock()
        m.get_memories.return_value = []
        return m

    def _run_node(self, llm, structured_llm, config):
        from tradingagents.agents.managers.risk_manager import create_risk_manager

        # Phase B replaced the AlpacaUtils prompt helpers with the shared
        # capture_position_context formatter; patch the same boundary.
        with patch(
            "tradingagents.agents.managers.risk_manager.capture_position_context"
        ) as capture, patch(
            "tradingagents.agents.managers.risk_manager.TradingMemoryLog"
        ) as log_cls, patch(
            "tradingagents.agents.managers.risk_manager.bind_structured",
            return_value=structured_llm,
        ):
            from types import SimpleNamespace

            capture.return_value = SimpleNamespace(
                symbol="NVDA", observed_at_iso="2026-01-02T00:00:00+00:00",
                account_id="paper-1", equity=100000.0, cash=100000.0,
                buying_power=100000.0, gross_exposure=0.0, gross_exposure_pct=0.0,
                qty=0.0, side="FLAT", market_value=None, avg_entry_price=None,
                unrealized_pl=None, unrealized_pl_pct=None,
                position_weight_pct=None, current_price=None,
            )
            log_cls.return_value.get_past_context.return_value = ""
            node = create_risk_manager(llm, self._memory(), config=config)
            return node(self._risk_state())

    def test_bind_failure_is_no_trade(self):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="BUY NVDA!!! FINAL TRANSACTION PROPOSAL: **BUY**")
        out = self._run_node(llm, None, config={"allow_shorts": False})
        self.assertIsNone(out["final_trade_intent"])
        self.assertIn("NO_TRADE", out["final_trade_decision"])
        self.assertEqual(out["recommended_action"], "HOLD")

    def test_invoke_timeout_and_provider_errors_are_no_trade(self):
        for exc in (
            TimeoutError("timed out"),
            ConnectionError("connection reset"),
            RuntimeError("429 rate limit"),
            RuntimeError("provider exploded"),
        ):
            llm = MagicMock()
            bad = MagicMock()
            bad.invoke.side_effect = exc
            out = self._run_node(llm, bad, config={"allow_shorts": False})
            self.assertIsNone(out["final_trade_intent"], msg=str(exc))
            self.assertIn("NO_TRADE", out["final_trade_decision"])

    def test_validation_failure_and_illegal_action_are_no_trade(self):
        from tradingagents.agents.schemas import ExecutableAction, RiskDecision

        # Validation failure: provider returns garbage dict.
        llm = MagicMock()
        garbage = MagicMock()
        garbage.invoke.return_value = {"not": "a risk decision"}
        out = self._run_node(llm, garbage, config={"allow_shorts": False})
        self.assertIsNone(out["final_trade_intent"])

        # Illegal cross-mode action: BUY in trading mode.
        illegal = MagicMock()
        illegal.invoke.return_value = RiskDecision(
            action=ExecutableAction.BUY,
            confidence="high",
            risk_rationale="x",
            required_controls="y",
        )
        out2 = self._run_node(llm, illegal, config={"allow_shorts": True})
        self.assertIsNone(out2["final_trade_intent"])
        self.assertIn("NO_TRADE", out2["final_trade_decision"])

    def test_missing_intent_is_fail_closed_with_zero_broker_calls(self):
        from tradingagents.execution import ExecutionService

        broker = _mock_broker()
        broker.submit_order.reset_mock()
        broker.close_position.reset_mock()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=_fresh_quote,
            )
            with patch("tradingagents.safety.get_safety_guard", return_value=_disabled_guard()):
                for bad_intent in (None, {}, {"symbol": "AAPL"}):
                    res = svc.execute(
                        trade_intent=bad_intent, dollar_amount=1000
                    )
                    self.assertFalse(res["success"])
                    self.assertEqual(res.get("broker_calls", -1), 0)
            broker.submit_order.assert_not_called()
            broker.close_position.assert_not_called()


class SafetyPassthroughTests(unittest.TestCase):
    def test_valid_intent_hits_deterministic_safety_gate(self):
        from tradingagents.execution import ExecutionService
        from tradingagents.safety import SafetyGuard

        broker = _mock_broker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=_fresh_quote,
            )
            # Real guard with a tiny notional cap must block before broker.
            guard = SafetyGuard(
                config={"safety_enabled": True, "max_trade_notional_usd": 10.0},
                state_path=Path(tmp) / "s.json",
                kill_switch_path=Path(tmp) / "KILL",
            )
            with patch(
                "tradingagents.safety.get_safety_guard", return_value=guard
            ):
                res = svc.execute(
                    trade_intent=_buy_intent(), dollar_amount=1000
                )
            self.assertFalse(res["success"])
            self.assertTrue(res.get("safety_blocked") or res.get("broker_calls") == 0)
            broker.submit_order.assert_not_called()


class DecisionIdempotencyTests(unittest.TestCase):
    def test_duplicate_decision_id_creates_single_intent(self):
        from tradingagents.execution import ExecutionService

        broker = _mock_broker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=_fresh_quote,
            )
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                first = svc.execute(
                    trade_intent=_buy_intent(),
                    decision_id="dec-dedup-1",
                    dollar_amount=1000,
                )
                second = svc.execute(
                    trade_intent=_buy_intent(),
                    decision_id="dec-dedup-1",
                    dollar_amount=1000,
                )
            self.assertEqual(first["intent_id"], second["intent_id"])
            self.assertTrue(second.get("deduped"))
            # Only the first call submitted; replay made zero new POSTs.
            self.assertEqual(broker.submit_order.call_count, 1)
            conn = sqlite3.connect(str(Path(tmp) / "execution.db"))
            try:
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM execution_intents WHERE decision_id='dec-dedup-1'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 1)

    def test_parallel_duplicate_decision_id_single_intent(self):
        from tradingagents.execution import ExecutionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(str(Path(tmp) / "execution.db"))
            payload = '{"a": 1}'
            errors: list = []

            def worker():
                try:
                    store.create_outbox(
                        decision_id="dec-parallel",
                        run_id=None,
                        symbol="AAPL",
                        action="BUY",
                        target_position="LONG",
                        payload_json=payload,
                        orders=[
                            {
                                "client_order_id": "ta-parallel-1",
                                "symbol": "AAPL",
                                "side": "buy",
                                "quantity": None,
                                "notional": 100.0,
                            }
                        ],
                    )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            conn = sqlite3.connect(str(Path(tmp) / "execution.db"))
            try:
                n_intent = conn.execute(
                    "SELECT COUNT(*) FROM execution_intents WHERE decision_id='dec-parallel'"
                ).fetchone()[0]
                n_order = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE client_order_id='ta-parallel-1'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n_intent, 1)
            self.assertEqual(n_order, 1)


class OrderIdempotencyTests(unittest.TestCase):
    def test_client_order_id_stable_across_restart_and_roles_distinct(self):
        from tradingagents.execution.store import client_order_id_for

        a = client_order_id_for("dec-1", "AAPL", "buy", role="open", seq=0)
        b = client_order_id_for("dec-1", "AAPL", "buy", role="open", seq=0)
        self.assertEqual(a, b)
        self.assertLessEqual(len(a), 48)
        close_id = client_order_id_for("dec-1", "AAPL", "sell", role="close", seq=0)
        open_id = client_order_id_for("dec-1", "AAPL", "buy", role="open", seq=1)
        self.assertNotEqual(close_id, open_id)
        stop_id = client_order_id_for("dec-1", "AAPL", "buy", role="protect-stop", seq=0)
        target_id = client_order_id_for("dec-1", "AAPL", "buy", role="protect-target", seq=0)
        self.assertNotEqual(stop_id, target_id)
        # No timestamps/randomness: allowed charset only.
        import re

        self.assertRegex(a, r"^[A-Za-z0-9\-]+$")


class DurableOutboxTests(unittest.TestCase):
    def test_db_commit_failure_makes_zero_broker_calls(self):
        from tradingagents.execution import ExecutionService

        broker = _mock_broker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=_fresh_quote,
            )
            with patch.object(
                svc.store, "create_outbox", side_effect=RuntimeError("disk full")
            ), patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                res = svc.execute(
                    trade_intent=_buy_intent(), dollar_amount=1000
                )
            self.assertFalse(res["success"])
            self.assertEqual(res.get("broker_calls"), 0)
            broker.submit_order.assert_not_called()
            broker.close_position.assert_not_called()

    def test_commit_then_crash_before_submit_leaves_recoverable_pending(self):
        from tradingagents.execution import ExecutionService
        from tradingagents.execution.store import client_order_id_for

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            svc = ExecutionService(db_path=db, broker_factory=lambda: _mock_broker(), quote_factory=_fresh_quote)
            # Simulate: durable commit happened, process died before submit.
            intent, orders, _ = svc.store.create_outbox(
                decision_id="dec-crash-1",
                run_id="run-1",
                symbol="AAPL",
                action="BUY",
                target_position="LONG",
                payload_json="{}",
                orders=[
                    {
                        "client_order_id": client_order_id_for(
                            "dec-crash-1", "AAPL", "buy", role="open", seq=0
                        ),
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": None,
                        "notional": 100.0,
                    }
                ],
            )
            # Restart: new service instance sees the same PENDING row.
            restarted = ExecutionService(db_path=db, broker_factory=lambda: _mock_broker(), quote_factory=_fresh_quote)
            pending = restarted.store.list_pending_orders()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["client_order_id"], orders[0]["client_order_id"])
            self.assertEqual(pending[0]["status"], "PENDING")


class UnknownRecoveryTests(unittest.TestCase):
    def test_submit_timeout_goes_unknown_with_single_post(self):
        from tradingagents.execution import ExecutionService

        broker = _mock_broker()
        broker.submit_order.side_effect = TimeoutError("submit timed out")
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=_fresh_quote,
            )
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                res = svc.execute(
                    trade_intent=_buy_intent(),
                    decision_id="dec-timeout-1",
                    dollar_amount=1000,
                )
            self.assertTrue(res.get("has_unknown"))
            self.assertEqual(broker.submit_order.call_count, 1)
            orders = res["orders"]
            self.assertEqual(orders[0]["status"], "UNKNOWN")

    def test_lookup_adopts_broker_order_without_second_logical_order(self):
        from tradingagents.execution import ExecutionService
        from tradingagents.execution.store import client_order_id_for

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "execution.db")
            broker = _mock_broker()
            broker.submit_order.side_effect = TimeoutError("timed out")
            svc = ExecutionService(db_path=db, broker_factory=lambda: broker, quote_factory=_fresh_quote)
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                res = svc.execute(
                    trade_intent=_buy_intent(),
                    decision_id="dec-adopt-1",
                    dollar_amount=1000,
                )
            client_oid = res["orders"][0]["client_order_id"]
            self.assertEqual(
                client_oid,
                client_order_id_for(
                    "dec-adopt-1", "AAPL", "buy", role="open", seq=0
                ),
            )
            # Broker actually holds the order; lookup must adopt it.
            lookup_broker = MagicMock()
            found = MagicMock()
            found.id = "broker-adopted-1"
            found.status = "accepted"
            found.filled_qty = 0
            lookup_broker.get_order_by_client_order_id.return_value = found
            svc2 = ExecutionService(db_path=db, broker_factory=lambda: lookup_broker, quote_factory=_fresh_quote)
            # Ensure lookup performs zero POSTs even if submit exists.
            if hasattr(lookup_broker, "submit_order"):
                lookup_broker.submit_order.reset_mock()
            out = svc2.lookup_unknown(client_oid)
            self.assertTrue(out["found"])
            self.assertEqual(out["order"]["broker_order_id"], "broker-adopted-1")
            if hasattr(lookup_broker, "submit_order"):
                lookup_broker.submit_order.assert_not_called()
            conn = sqlite3.connect(db)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE client_order_id=?",
                    (client_oid,),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 1)


class FillAndTerminalTests(unittest.TestCase):
    def test_partial_fill_and_duplicate_fill_idempotent(self):
        from tradingagents.execution import ExecutionService

        broker = _mock_broker(status="accepted")
        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: broker,
                quote_factory=_fresh_quote,
            )
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                res = svc.execute(
                    trade_intent=_buy_intent(),
                    decision_id="dec-fill-1",
                    dollar_amount=1000,
                )
            order_id = res["orders"][0]["order_id"]
            is_new, row = svc.store.record_fill(
                execution_id="exec-1", order_id=order_id, qty=2, price=100.0
            )
            self.assertTrue(is_new)
            self.assertEqual(row["status"], "PARTIAL")
            self.assertEqual(row["filled_qty"], 2.0)
            # Duplicate replay must not double-count.
            is_new2, row2 = svc.store.record_fill(
                execution_id="exec-1", order_id=order_id, qty=2, price=100.0
            )
            self.assertFalse(is_new2)
            self.assertEqual(row2["filled_qty"], 2.0)
            # Second distinct fill accumulates on the same order (no補单).
            _, row3 = svc.store.record_fill(
                execution_id="exec-2", order_id=order_id, qty=3, price=101.0
            )
            self.assertEqual(row3["filled_qty"], 5.0)
            n_orders = len(svc.store.list_orders_for_intent(res["intent_id"]))
            self.assertEqual(n_orders, 1)
            # Terminal transition persists.
            ok, terminal = svc.store.transition_order(order_id, "FILLED")
            self.assertTrue(ok)
            self.assertEqual(terminal["status"], "FILLED")

    def test_all_terminal_states_persist(self):
        from tradingagents.execution import ExecutionService

        for terminal in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            with tempfile.TemporaryDirectory() as tmp:
                svc = ExecutionService(
                    db_path=str(Path(tmp) / "execution.db"),
                    broker_factory=lambda: _mock_broker(),
                    quote_factory=_fresh_quote,
                )
                intent, orders, _ = svc.store.create_outbox(
                    decision_id=f"dec-term-{terminal}",
                    run_id=None,
                    symbol="AAPL",
                    action="BUY",
                    target_position="LONG",
                    payload_json="{}",
                    orders=[
                        {
                            "client_order_id": f"ta-term-{terminal}",
                            "symbol": "AAPL",
                            "side": "buy",
                            "quantity": None,
                            "notional": 10.0,
                        }
                    ],
                )
                oid = orders[0]["order_id"]
                svc.store.transition_order(oid, "SUBMITTING")
                ok, row = svc.store.transition_order(oid, terminal)
                self.assertTrue(ok, msg=terminal)
                self.assertEqual(row["status"], terminal)


class StateMachineTests(unittest.TestCase):
    def test_legal_transitions_accepted_and_illegal_rejected(self):
        from tradingagents.execution import ExecutionService
        from tradingagents.execution.store import is_valid_order_transition

        # Spot-check the central validator.
        self.assertTrue(is_valid_order_transition("PENDING", "SUBMITTING"))
        self.assertTrue(is_valid_order_transition("SUBMITTING", "UNKNOWN"))
        self.assertTrue(is_valid_order_transition("UNKNOWN", "ACCEPTED"))
        self.assertTrue(is_valid_order_transition("ACCEPTED", "PARTIAL"))
        self.assertTrue(is_valid_order_transition("PARTIAL", "FILLED"))
        self.assertFalse(is_valid_order_transition("PENDING", "UNKNOWN"))
        self.assertFalse(is_valid_order_transition("PENDING", "FILLED"))
        self.assertFalse(is_valid_order_transition("FILLED", "PARTIAL"))
        self.assertFalse(is_valid_order_transition("CANCELED", "ACCEPTED"))

        with tempfile.TemporaryDirectory() as tmp:
            svc = ExecutionService(
                db_path=str(Path(tmp) / "execution.db"),
                broker_factory=lambda: _mock_broker(),
                quote_factory=_fresh_quote,
            )
            _, orders, _ = svc.store.create_outbox(
                decision_id="dec-sm-1",
                run_id=None,
                symbol="AAPL",
                action="BUY",
                target_position="LONG",
                payload_json="{}",
                orders=[
                    {
                        "client_order_id": "ta-sm-1",
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": None,
                        "notional": 10.0,
                    }
                ],
            )
            oid = orders[0]["order_id"]
            ok, _ = svc.store.transition_order(oid, "FILLED")
            self.assertFalse(ok)
            cur = svc.store.get_order(oid)
            assert cur is not None
            self.assertEqual(cur["status"], "PENDING")
            # Terminal never returns to nonterminal.
            svc.store.transition_order(oid, "SUBMITTING")
            svc.store.transition_order(oid, "FILLED")
            ok2, cur2 = svc.store.transition_order(oid, "PARTIAL")
            self.assertFalse(ok2)
            self.assertEqual(cur2["status"], "FILLED")


class SingleEntryTests(unittest.TestCase):
    def test_production_callers_use_single_execution_entry(self):
        repo = Path(__file__).resolve().parent.parent
        analysis = (repo / "webui" / "components" / "analysis.py").read_text()
        trading_cb = (repo / "webui" / "callbacks" / "trading_callbacks.py").read_text()
        # No legacy signal fallback: analysis must not call the raw signal path.
        self.assertNotIn("execute_trading_action(", analysis)
        # Production liquidation must go through the service, not raw close.
        self.assertIn("ExecutionService", trading_cb)
        self.assertNotIn("AlpacaUtils.close_position(", trading_cb)
        self.assertIn("ExecutionService", analysis)
        # No production module may call the removed direct helpers or the
        # disabled signal path (tests/ and the deprecated wrapper itself
        # excluded; the wrapper delegates without broker calls).
        offenders = []
        for src in list((repo / "tradingagents").rglob("*.py")) + list(
            (repo / "webui").rglob("*.py")
        ) + list((repo / "cli").rglob("*.py")):
            text = src.read_text()
            rel = str(src.relative_to(repo))
            if "place_market_order(" in text or "place_protected_market_order(" in text:
                offenders.append(f"{rel}: direct place helper call")
            if "AlpacaUtils.close_position(" in text:
                offenders.append(f"{rel}: direct close call")
            if "execute_trading_action(" in text and src.name != "alpaca_utils.py":
                offenders.append(f"{rel}: legacy signal execution call")
        self.assertEqual(offenders, [])
        # Unique constraints are the last line of defense.
        store_src = (
            repo / "tradingagents" / "execution" / "store.py"
        ).read_text()
        self.assertIn("decision_id TEXT UNIQUE", store_src)
        self.assertIn("client_order_id TEXT UNIQUE", store_src)
        self.assertIn("broker_order_id TEXT UNIQUE", store_src)
        self.assertIn("execution_id TEXT PRIMARY KEY", store_src)

    def test_legacy_signal_path_is_fail_closed_with_zero_broker_calls(self):
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        broker = MagicMock()
        with patch(
            "tradingagents.dataflows.alpaca_utils.get_alpaca_trading_client",
            return_value=broker,
        ):
            for signal in ("BUY", "SELL", "LONG", "SHORT", "HOLD", "NEUTRAL"):
                res = AlpacaUtils.execute_trading_action(
                    symbol="AAPL",
                    current_position="NEUTRAL",
                    signal=signal,
                    dollar_amount=1000,
                    allow_shorts=True,
                )
                self.assertFalse(res["success"])
                self.assertEqual(res.get("broker_calls"), 0)
                self.assertFalse(res.get("broker_attempted", False))
        broker.submit_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_direct_mutation_helpers_are_removed(self):
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        for name in (
            "place_market_order",
            "place_protected_market_order",
            "close_position",
        ):
            self.assertFalse(
                hasattr(AlpacaUtils, name), msg=f"AlpacaUtils.{name} must be removed"
            )

    def test_wrapper_delegates_to_durable_service(self):
        broker = _mock_broker()
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=_disabled_guard(),
            ):
                from tradingagents.dataflows.alpaca_utils import AlpacaUtils

                res = AlpacaUtils.execute_trade_intent(
                    symbol="AAPL",
                    current_position="NEUTRAL",
                    trade_intent=_buy_intent(),
                    dollar_amount=1000,
                    allow_shorts=False,
                    db_path=str(Path(tmp) / "execution.db"),
                    broker_factory=lambda: broker,
                    quote_factory=_fresh_quote,
                )
            self.assertTrue(res["success"])
            self.assertEqual(broker.submit_order.call_count, 1)
            request = broker.submit_order.call_args[0][0]
            self.assertIsNotNone(getattr(request, "client_order_id", None))


if __name__ == "__main__":
    unittest.main()
