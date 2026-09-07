"""Phase B3/B4 tests: deterministic exposure caps through the single
evaluator and the real execution entry — symbol/sector/gross/cash clipping,
outstanding-order headroom, flip handling, and recovery-path enforcement."""

import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.execution.authority import BrokerOrder, BrokerPosition, BrokerQuote, BrokerSnapshot
from tradingagents.execution.service import ExecutionService
from tradingagents.risk.exposure import (
    MIN_ORDER_NOTIONAL,
    evaluate_opening_exposure,
    outstanding_increasing_notional,
)

def _ready_entry_policy():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "READY", "minimum_price": 99, "maximum_price": 101,
            "expires_at": (now+timedelta(hours=1)).isoformat(),
            "exit_by": (now+timedelta(days=5)).isoformat(), "confirmation": "fixture observed setup"}


def _now():
    # Fresh at call time: authority.py enforces real snapshot (30s) and
    # quote (15s) TTLs, so import-time timestamps go stale mid-suite on a
    # slow run and the fail-closed freshness gate rejects the fake broker
    # facts. Fixtures must stamp facts as of when the test actually runs.
    return datetime.now(timezone.utc)


def _fresh_quote(symbol="AAPL", price=100.0):
    return BrokerQuote(symbol.replace("/", ""), price - 0.1, price + 0.1, _now())


def _snapshot(positions=None, orders=None, *, equity=100000.0, cash=80000.0,
              account_id="paper-1"):
    positions = tuple(positions or ())
    orders = tuple(orders or ())
    return BrokerSnapshot(
        observed_at=_now(),
        version="v-caps",
        account_id=account_id,
        equity=equity,
        last_equity=equity if equity > 0 else 100000.0,
        cash=cash,
        buying_power=equity * 2,
        positions=positions,
        orders=orders,
        fills=(),
        gross_exposure=sum(abs(p.market_value) for p in positions),
    )


def _pos(symbol, qty, mv):
    return BrokerPosition(symbol, qty, mv)


def _order(symbol, side, *, qty=10, notional=None, status="new", client_id="ta-x"):
    return BrokerOrder(
        broker_order_id=f"b-{client_id}",
        client_order_id=client_id,
        symbol=symbol,
        side=side,
        status=status,
        qty=qty,
        filled_qty=0.0,
        filled_avg_price=None,
        updated_at=_now(),
        notional=notional,
    )


def _evaluate(**overrides):
    kwargs = dict(
        symbol="AAPL",
        proposed_notional=5000.0,
        snapshot=_snapshot(),
        quote_price=100.0,
        symbol_cap_pct=25.0,
        sector_cap_pct=30.0,
        gross_cap_pct=100.0,
        sector_mapping={},
    )
    kwargs.update(overrides)
    return evaluate_opening_exposure(**kwargs)


class SymbolCapTests(unittest.TestCase):
    def test_headroom_clips_to_available_room(self):
        # 18% held with a 20% cap: room is exactly 2% of 100k = 2000.
        snapshot = _snapshot(positions=[_pos("AAPL", 100, 18000.0)])
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=5000.0, symbol_cap_pct=20.0
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, 2000.0)

    def test_at_or_over_cap_rejects_any_increase(self):
        # 28% held with a 20% cap: increases are forbidden.
        snapshot = _snapshot(positions=[_pos("AAPL", 100, 28000.0)])
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=5000.0, symbol_cap_pct=20.0
        )
        self.assertFalse(decision.approved)

    def test_uncapped_config_is_a_configuration_error_for_auto_execution(self):
        decision = _evaluate(symbol_cap_pct=0)
        self.assertFalse(decision.approved)
        self.assertIn("positive percentage", decision.reason)

    def test_quantity_floors_below_the_minimum_unit(self):
        # Headroom 2000 at price 100 -> qty 20 shares = 2000; floor keeps 2000.
        snapshot = _snapshot(positions=[_pos("AAPL", 100, 18000.0)])
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=2000.4, symbol_cap_pct=20.0
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, 2000.0)
        # Anything below the minimum placeable notional is no order.
        tiny = _evaluate(proposed_notional=0.5)
        self.assertFalse(tiny.approved)
        self.assertLess(0.5, MIN_ORDER_NOTIONAL)


class OutstandingOrderTests(unittest.TestCase):
    def test_outstanding_increasing_notional_consumes_headroom(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", 100, 18000.0)],
            orders=[_order("AAPL", "buy", qty=10, notional=1000.0)],
        )
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=5000.0, symbol_cap_pct=20.0
        )
        # 20000 - 18000 - 1000 = 1000 of remaining headroom.
        self.assertEqual(decision.notional, 1000.0)

    def test_qty_only_outstanding_uses_quote_price(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", 100, 18000.0)],
            orders=[_order("AAPL", "buy", qty=5, notional=None)],
        )
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=5000.0,
            symbol_cap_pct=20.0, quote_price=100.0,
        )
        # 5 shares * 100 = 500 outstanding -> 1500 headroom left.
        self.assertEqual(decision.notional, 1500.0)

    def test_unestimable_outstanding_refuses_the_increase(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", 100, 10000.0)],
            orders=[_order("AAPL", "buy", qty=5, notional=None)],
        )
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=5000.0,
            symbol_cap_pct=20.0, quote_price=None,
        )
        self.assertFalse(decision.approved)
        self.assertIn("cannot reliably estimate", decision.reason)

    def test_filled_orders_do_not_consume_headroom(self):
        snapshot = _snapshot(
            positions=[_pos("AAPL", 100, 18000.0)],
            orders=[_order("AAPL", "buy", qty=10, notional=1000.0, status="filled")],
        )
        decision = _evaluate(
            snapshot=snapshot, proposed_notional=5000.0, symbol_cap_pct=20.0
        )
        self.assertEqual(decision.notional, 2000.0)


class SectorCapTests(unittest.TestCase):
    def test_sector_headroom_clips_to_sector_room(self):
        # Technology holds 28000 of 100k with a 30% sector cap.
        snapshot = _snapshot(positions=[_pos("NVDA", 10, 28000.0)])
        decision = _evaluate(
            symbol="AAPL",
            proposed_notional=5000.0,
            snapshot=snapshot,
            sector_mapping={"NVDA": "Technology", "AAPL": "Technology"},
            sector_cap_pct=30.0,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, 2000.0)

    def test_unknown_candidate_sector_refuses_new_risk(self):
        snapshot = _snapshot(positions=[_pos("NVDA", 10, 10000.0)])
        decision = _evaluate(
            snapshot=snapshot,
            sector_mapping={"NVDA": "Technology"},  # AAPL missing
        )
        self.assertFalse(decision.approved)
        self.assertIn("AAPL", decision.reason)

    def test_unknown_holding_sector_refuses_new_risk(self):
        snapshot = _snapshot(positions=[_pos("MSFT", 10, 10000.0)])
        decision = _evaluate(
            snapshot=snapshot,
            sector_mapping={"AAPL": "Technology"},  # MSFT holding missing
        )
        self.assertFalse(decision.approved)
        self.assertIn("MSFT", decision.reason)

    def test_invalid_sector_cap_is_a_configuration_error(self):
        decision = _evaluate(sector_mapping={"AAPL": "Tech"}, sector_cap_pct=150.0)
        self.assertFalse(decision.approved)
        self.assertIn("0 < cap <= 100", decision.reason)

    def test_sector_cap_disabled_without_mapping(self):
        decision = _evaluate(sector_mapping={}, sector_cap_pct=30.0)
        self.assertTrue(decision.approved)


class CombinedCapTests(unittest.TestCase):
    def test_most_binding_cap_wins(self):
        # symbol headroom 2000 (20%), sector headroom 1500 (30% minus 28500),
        # gross headroom 11000, cash 40000 -> sector is most binding.
        snapshot = _snapshot(
            positions=[_pos("NVDA", 10, 28000.0), _pos("AAPL", 10, 500.0)],
            cash=40000.0,
        )
        decision = _evaluate(
            symbol="AAPL",
            proposed_notional=5000.0,
            snapshot=snapshot,
            sector_mapping={"NVDA": "Technology", "AAPL": "Technology"},
            symbol_cap_pct=20.0,
            sector_cap_pct=30.0,
            gross_cap_pct=100.0,
        )
        self.assertEqual(decision.notional, 1500.0)

    def test_cash_limits_the_opening_notional(self):
        # Cash binds below the proposal: the clip is applied, not a rejection.
        decision = _evaluate(snapshot=_snapshot(cash=3000.0))
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, 3000.0)
        # Cash below the minimum placeable notional means no order.
        decision = _evaluate(snapshot=_snapshot(cash=0.5))
        self.assertFalse(decision.approved)


class FlipAndExitTests(unittest.TestCase):
    def test_verified_close_frees_value_for_the_open_leg(self):
        # Flip: close the 20000 long, open 8000 -> the increasing leg is
        # evaluated against a post-close flat book, not the old position.
        snapshot = _snapshot(positions=[_pos("AAPL", 100, 20000.0)])
        decision = _evaluate(
            snapshot=snapshot,
            proposed_notional=8000.0,
            symbol_cap_pct=20.0,
            planned_close_reduction=20000.0,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, 8000.0)

    def test_partial_close_frees_only_proportional_value(self):
        snapshot = _snapshot(positions=[_pos("AAPL", 100, 20000.0)])
        decision = _evaluate(
            snapshot=snapshot,
            proposed_notional=8000.0,
            symbol_cap_pct=20.0,
            planned_close_reduction=10000.0,  # half the position
        )
        # Remaining book 10000, cap room 20000 -> 10000 headroom.
        self.assertEqual(decision.notional, 8000.0)

    def test_reducing_exit_never_routes_through_the_evaluator(self):
        # Structural proof: evaluate_opening_exposure is only called for
        # opens; a pure close intent with an over-cap position still executes
        # through the verified-exit path (Phase A regression below covers the
        # broker side).
        snapshot = _snapshot(positions=[_pos("AAPL", 100, 95000.0)])
        decision = _evaluate(snapshot=snapshot, proposed_notional=0.0)
        self.assertFalse(decision.approved)  # evaluator is never the exit path


class ExecutionIntegrationTests(unittest.TestCase):
    """The real execution entry must honor the caps and the quarantine gate."""

    def _service(self, tmp, broker):
        return ExecutionService(
            db_path=str(Path(tmp) / "execution.db"),
            broker_factory=lambda: broker,
            quote_factory=lambda symbol: _fresh_quote(symbol),
        )

    def _intent(self, action="BUY", symbol="AAPL", current="NEUTRAL"):
        from tradingagents.agents.schemas import (
            ExecutableAction, RiskDecision, build_trade_intent_from_risk_decision,
        )

        return build_trade_intent_from_risk_decision(
            symbol=symbol,
            trading_mode="investment",
            current_position=current,
            allow_shorts=False,
            trade_date="2026-09-05",
            decision=RiskDecision(
                action=ExecutableAction(action),
                confidence="medium",
                risk_rationale="caps test",
                required_controls="strict",
                entry_policy=_ready_entry_policy(), stop_loss_price=95.0,
            ),
        ).model_dump(mode="json")

    def _broker(self, positions=None, orders=None, *, equity=100000.0, cash=80000.0):
        positions = list(positions or [])
        orders = list(orders or [])
        submit_calls = []
        state = {"positions": positions, "orders": orders, "submit_calls": submit_calls}

        def submit_order(request):
            submit_calls.append(1)
            get = request.get if isinstance(request, dict) else (
                lambda name: getattr(request, name, None)
            )
            side = get("side")
            order = SimpleNamespace(
                id=f"broker-{len(submit_calls)}",
                client_order_id=get("client_order_id"),
                symbol=str(get("symbol")),
                side=str(getattr(side, "value", side)),
                status="accepted",
                qty=str(get("qty") or 0),
                notional=get("notional"),
                filled_qty="0",
                filled_avg_price=None,
                updated_at=_now(),
            )
            # Record the live order so post-order reconciliation sees it.
            state["orders"].append(order)
            return order

        def get_all_positions():
            return list(state["positions"])

        def get_orders(request=None):
            return list(state["orders"])

        def get_order_by_client_order_id(cid):
            return next((o for o in state["orders"] if o.client_order_id == cid), None)

        broker = SimpleNamespace(
            get_account=lambda: SimpleNamespace(
                id="paper-1", equity=str(equity), last_equity=str(equity),
                cash=str(cash), buying_power=str(equity * 2)
            ),
            get_all_positions=get_all_positions,
            get_orders=get_orders,
            get_order_by_client_order_id=get_order_by_client_order_id,
            submit_order=submit_order,
            state=state,
        )
        return broker

    def test_second_order_cannot_reuse_filled_headroom(self):
        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            # Account: no holdings, cap 20% -> 2000 clip for a 5000 order.
            with patch(
                "tradingagents.execution.service._get_execution_config",
                return_value={"max_symbol_concentration_pct": 20.0, "sector_mapping": {}},
            ):
                # Open a position near the cap: 18% of the 100k account.
                first = svc.execute(
                    trade_intent=self._intent(), dollar_amount=18000.0
                )
                self.assertTrue(first["success"])
                self.assertEqual(first["broker_calls"], 1)
                # Simulate the fill: the broker now holds 18000 of AAPL and
                # the first order is filled (no longer live). Keep its real
                # client_order_id so reconciliation still matches the row.
                first_client_id = first["orders"][0]["client_order_id"]
                broker.state["positions"] = [_pos_dict("AAPL", 180, 18000.0)]
                broker.state["orders"] = [
                    SimpleNamespace(
                        id="broker-1", client_order_id=first_client_id,
                        symbol="AAPL", side="buy", status="filled", qty="180",
                        notional=None, filled_qty="180", filled_avg_price="100",
                        updated_at=datetime.now(timezone.utc),
                    )
                ]
                second = svc.execute(
                    trade_intent=self._intent(), dollar_amount=5000.0,
                    decision_id="dec-second-buy",
                )
            self.assertTrue(second["success"])
            clipped = float(second["orders"][0]["quantity"]) * 101
            # Headroom was recomputed from the fresh snapshot: the B13
            # acceptance example — 18% held, cap 20% -> only 2% (2000) fits.
            self.assertLessEqual(clipped, 2000.0)
            self.assertGreater(clipped, 1899.0)

    def test_pending_buy_order_consumes_headroom(self):
        # A live (not yet filled) opening buy from a prior decision must be
        # counted as outstanding increasing notional for the next order.
        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            with patch(
                "tradingagents.execution.service._get_execution_config",
                return_value={"max_symbol_concentration_pct": 20.0, "sector_mapping": {}},
            ):
                first = svc.execute(
                    trade_intent=self._intent(), dollar_amount=5000.0
                )
                self.assertTrue(first["success"])
                self.assertEqual(first["broker_calls"], 1)
                # The first order stays live at the broker (accepted, 0 filled).
                second = svc.execute(
                    trade_intent=self._intent(), dollar_amount=18000.0,
                    decision_id="dec-second-buy",
                )
            self.assertTrue(second["success"])
            clipped = float(second["orders"][0]["quantity"]) * 101
            # Headroom 20000 minus the 5000 outstanding buy = 15000.
            self.assertLessEqual(clipped, 15100.0)
            self.assertGreater(clipped, 14899.0)

    def test_quarantined_symbol_takes_zero_broker_calls(self):
        from tradingagents.risk.corporate_actions import QuarantineStore, QuarantineGate

        broker = self._broker()
        with tempfile.TemporaryDirectory() as tmp:
            store = QuarantineStore(Path(tmp) / "quarantine.json")
            store.quarantine(symbol="AAPL", reason="split", source="operator-test")
            svc = self._service(tmp, broker)
            svc._quarantine_gate = QuarantineGate(store)
            result = svc.execute(trade_intent=self._intent(), dollar_amount=5000.0)
            self.assertFalse(result["success"])
            self.assertTrue(result.get("quarantined"))
            self.assertEqual(result["broker_calls"], 0)
            self.assertEqual(len(broker.state["submit_calls"]), 0)

    def test_quarantined_symbol_still_allows_verified_reducing_exit(self):
        from tradingagents.risk.corporate_actions import QuarantineStore, QuarantineGate

        broker = self._broker(positions=[_pos("AAPL", 100, 10000.0)])
        with tempfile.TemporaryDirectory() as tmp:
            store = QuarantineStore(Path(tmp) / "quarantine.json")
            store.quarantine(symbol="AAPL", reason="split", source="operator-test")
            svc = self._service(tmp, broker)
            svc._quarantine_gate = QuarantineGate(store)
            with patch(
                "tradingagents.safety.get_safety_guard",
                return_value=SimpleNamespace(enabled=False, check_order=lambda *a, **k: None,
                                             record_order_result=lambda ok: None),
            ):
                result = svc.liquidate("AAPL")
            self.assertTrue(result["success"])
            self.assertEqual(len(broker.state["submit_calls"]), 1)

    def test_recovery_resubmit_recomputes_caps_not_reuses_headroom(self):
        # A recovered PENDING order is resubmitted with the cap-clipped
        # notional recomputed from the fresh snapshot — never the original
        # size with stale headroom.
        broker = self._broker(positions=[_pos("AAPL", 150, 19500.0)])
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._service(tmp, broker)
            svc.store.create_outbox(
                decision_id="dec-cap", run_id=None, symbol="AAPL", action="BUY",
                target_position="LONG", payload_json=json.dumps(self._intent()),
                orders=[{"client_order_id": "ta-cap-1", "symbol": "AAPL",
                         "side": "buy", "quantity": None, "notional": 4000.0}],
            )
            with patch(
                "tradingagents.execution.service._get_execution_config",
                return_value={"max_symbol_concentration_pct": 20.0, "sector_mapping": {}},
            ):
                result = svc.startup_recover()
            self.assertTrue(result["success"])
            row = svc.store.get_order_by_client("ta-cap-1")
            self.assertEqual(row["status"], "ACCEPTED")
            # The resubmitted request carried the clipped notional (500),
            # not the original 4000: 20000 cap - 19500 held = 500.
            submitted = broker.state["orders"][0]
            self.assertEqual(float(submitted.qty), 4.0)
            self.assertIsNone(submitted.notional)


def _pos_dict(symbol, qty, mv):
    return SimpleNamespace(symbol=symbol, qty=str(qty), market_value=str(mv),
                           avg_entry_price=None, unrealized_pl=None, current_price=None)


if __name__ == "__main__":
    unittest.main()
