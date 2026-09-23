"""F05: deterministic aggregate planned-stop-risk budget, offline only."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tradingagents.execution.authority import (
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
)
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.store import ExecutionStore
from tradingagents.risk.exposure import (
    MIN_ORDER_NOTIONAL,
    clip_to_portfolio_stop_risk,
    remaining_position_stop_risk,
)


def _payload(symbol="AAPL", *, target_position="LONG", stop=90.0, target=120.0,
             low=98.0, high=100.0):
    short = target_position == "SHORT"
    return {
        "symbol": symbol,
        "trade_date": "2026-09-22",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "action": "SHORT" if short else "BUY",
        "current_position": "NEUTRAL",
        "target_position": target_position,
        "position_transition": "OPEN_SHORT" if short else "OPEN_LONG",
        "planned_actions": [{
            "action": "open_short" if short else "open_long",
            "order_type": "market", "side": "sell" if short else "buy",
            "sizing_basis": "configured_notional",
        }],
        "order_intent": {"order_type": "market", "side": "sell" if short else "buy",
                          "sizing_basis": "configured_notional"},
        "entry_policy": {
            "status": "READY", "minimum_price": low, "maximum_price": high,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "exit_by": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
            "risk_fraction": 0.01, "confirmation": "observed setup",
        },
        "risk_controls": {
            "stop_loss_price": stop, "take_profit_price": target,
            "invalidation": "original setup invalidated",
        },
        "time_horizon": "5 days",
        "rationale_summary": f"original {target_position.lower()} thesis for {symbol}",
    }


def _snapshot(positions=(), orders=(), *, equity=100000.0, cash=90000.0):
    now = datetime.now(timezone.utc)
    positions = tuple(positions)
    orders = tuple(orders)
    return BrokerSnapshot(
        observed_at=now, version="stop-risk-test", account_id="paper-risk",
        equity=equity, last_equity=equity, cash=cash, buying_power=equity,
        positions=positions, orders=orders, fills=(),
        gross_exposure=sum(abs(p.market_value) for p in positions),
    )


def _position(symbol, qty, mark, *, current_price=True):
    return BrokerPosition(
        symbol=symbol, qty=qty, market_value=qty * mark,
        current_price=mark if current_price else None,
    )


def _write_order(store, decision_id, symbol, payload, *, quantity=None, notional=None,
                 fill_qty=0.0, fill_price=100.0, status=None):
    _, rows, _ = store.create_outbox(
        decision_id=decision_id, run_id=None, symbol=symbol,
        action=payload["action"], target_position=payload["target_position"],
        payload_json=__import__("json").dumps(payload),
        orders=[{
            "client_order_id": f"local-{decision_id}", "symbol": symbol,
            "side": "sell" if payload["target_position"] == "SHORT" else "buy",
            "quantity": quantity, "notional": notional,
        }],
    )
    order = rows[0]
    if fill_qty:
        store.record_fill(
            execution_id=f"fill-{decision_id}", order_id=order["order_id"],
            qty=fill_qty, price=fill_price,
        )
    if status == "FILLED":
        store.transition_order(order["order_id"], "FILLED")
    elif status in {"CANCELED", "REJECTED", "EXPIRED"}:
        store.transition_order(order["order_id"], "SUBMITTING")
        store.transition_order(order["order_id"], status)
    return store.get_order(order["order_id"])


def _evaluate(store, snapshot, payload=None, *, proposed=10000.0, candidate_order_id=None):
    from tradingagents.execution import service as service_module

    symbol = (payload or _payload())["symbol"]
    quote = BrokerQuote(symbol, 99.9, 100.1, datetime.now(timezone.utc))
    with patch(
        "tradingagents.execution.service._get_execution_config",
        return_value={
            "max_symbol_concentration_pct": 100.0,
            "portfolio_max_gross_exposure_pct": 100.0,
            "portfolio_max_stop_risk_pct": 5.0,
            "sector_mapping": {},
        },
    ):
        return service_module._evaluate_opening_caps(
            symbol=symbol,
            specs=[{"role": "open", "notional": proposed, "quantity": None}],
            snapshot=snapshot,
            quote=quote,
            intent_dict=payload or _payload(),
            quote_factory=lambda wanted: BrokerQuote(
                wanted, 99.9, 100.1, datetime.now(timezone.utc)
            ),
            execution_store=store,
            candidate_order_id=candidate_order_id,
        )


class TestPureStopRiskArithmetic:
    def test_remaining_stop_risk_is_directional_for_long_and_short(self):
        assert remaining_position_stop_risk(qty=10, current_mark=100, stop_loss_price=90) == 100
        assert remaining_position_stop_risk(qty=-10, current_mark=100, stop_loss_price=110) == 100

    @pytest.mark.parametrize("kwargs", [
        {"qty": 10, "current_mark": float("nan"), "stop_loss_price": 90},
        {"qty": 10, "current_mark": 100, "stop_loss_price": -1},
        {"qty": 0, "current_mark": 100, "stop_loss_price": 90},
        {"qty": True, "current_mark": 100, "stop_loss_price": 90},
    ])
    def test_malformed_remaining_stop_risk_fails_closed(self, kwargs):
        with pytest.raises(ValueError):
            remaining_position_stop_risk(**kwargs)

    def test_budget_accepts_headroom_and_clips_above_budget(self):
        allowed = clip_to_portfolio_stop_risk(
            proposed_notional=5000, equity=100000, max_stop_risk_pct=5,
            existing_position_risk=1000, reserved_pending_risk=1000,
            candidate_risk_fraction=0.1,
        )
        assert allowed.approved and allowed.notional == 5000
        clipped = clip_to_portfolio_stop_risk(
            proposed_notional=10000, equity=100000, max_stop_risk_pct=5,
            existing_position_risk=4800, reserved_pending_risk=0,
            candidate_risk_fraction=0.1,
        )
        assert clipped.approved and clipped.notional == 2000

    def test_below_minimum_and_malformed_budget_inputs_reject(self):
        too_small = clip_to_portfolio_stop_risk(
            proposed_notional=10000, equity=100000, max_stop_risk_pct=5,
            existing_position_risk=4999.95, reserved_pending_risk=0,
            candidate_risk_fraction=0.1,
        )
        assert not too_small.approved and too_small.notional == 0
        assert MIN_ORDER_NOTIONAL == 1.0
        bad_rows = [
            {"equity": float("nan")},
            {"existing_position_risk": -1},
            {"reserved_pending_risk": -1},
            {"candidate_risk_fraction": 0},
            {"max_stop_risk_pct": -5},
            {"proposed_notional": True},
        ]
        base = dict(
            proposed_notional=1000, equity=100000, max_stop_risk_pct=5,
            existing_position_risk=0, reserved_pending_risk=0,
            candidate_risk_fraction=0.1,
        )
        for change in bad_rows:
            assert not clip_to_portfolio_stop_risk(**(base | change)).approved


class TestStopRiskExecutionBridge:
    def _store(self, tmp_path):
        store = ExecutionStore(tmp_path / "execution.sqlite3")
        store.ensure_account_binding("paper-risk")
        return store

    def test_flat_candidate_under_budget_passes_and_long_risk_is_added(self, tmp_path):
        store = self._store(tmp_path)
        candidate = _payload("AAPL")
        no_holdings = _evaluate(store, _snapshot(), candidate, proposed=10000)
        assert no_holdings.approved and no_holdings.notional == 10000

        _write_order(store, "long-hold", "AAPL", _payload("AAPL"),
                     quantity=10, fill_qty=10, fill_price=100, status="FILLED")
        held = _evaluate(
            store, _snapshot([_position("AAPL", 10, 100)]), candidate, proposed=10000,
        )
        assert held.approved
        assert held.details["existing_position_risk"] == 100

    def test_multiple_long_and_mixed_long_short_risk_sum(self, tmp_path):
        store = self._store(tmp_path)
        _write_order(store, "long-a", "AAPL", _payload("AAPL", stop=90, target=120),
                     quantity=10, fill_qty=10, fill_price=100, status="FILLED")
        _write_order(store, "long-b", "MSFT", _payload("MSFT", stop=40, target=70, low=48, high=50),
                     quantity=20, fill_qty=20, fill_price=50, status="FILLED")
        snapshot = _snapshot([
            _position("AAPL", 10, 100),
            _position("MSFT", 20, 50),
        ])
        result = _evaluate(store, snapshot, _payload("GOOG"), proposed=10000)
        assert result.approved
        assert result.details["existing_position_risk"] == 300
        _write_order(store, "short-c", "TSLA", _payload("TSLA", target_position="SHORT",
                     stop=60, target=40, low=48, high=50), quantity=20,
                     fill_qty=20, fill_price=50, status="FILLED")
        mixed = _evaluate(
            store,
            _snapshot([
                _position("AAPL", 10, 100), _position("MSFT", 20, 50),
                _position("TSLA", -20, 50),
            ]),
            _payload("GOOG"), proposed=10000,
        )
        assert mixed.approved
        assert mixed.details["existing_position_risk"] == 500

    def test_existing_risk_clips_candidate_and_minimum_notional_rejects(self, tmp_path):
        store = self._store(tmp_path)
        _write_order(store, "large-risk", "AAPL", _payload("AAPL", stop=60, target=140),
                     quantity=120, fill_qty=120, fill_price=100, status="FILLED")
        clipped = _evaluate(
            store, _snapshot([_position("AAPL", 120, 100)]), _payload("AAPL"),
            proposed=10000,
        )
        assert clipped.approved and clipped.notional == 2000
        assert clipped.details["existing_position_risk"] == 4800

        tiny_store = self._store(tmp_path / "tiny")
        quantity = 499.995
        _write_order(tiny_store, "near-budget", "AAPL", _payload("AAPL"),
                     quantity=quantity, fill_qty=quantity, fill_price=100, status="FILLED")
        rejected = _evaluate(
            tiny_store, _snapshot([_position("AAPL", quantity, 100)]), _payload("AAPL"),
            proposed=10000,
        )
        assert not rejected.approved
        assert "below the $1.00 minimum" in rejected.reason

    def test_short_stop_risk_and_provable_market_value_mark_fallback(self, tmp_path):
        store = self._store(tmp_path)
        short = _payload("AAPL", target_position="SHORT", stop=110, target=70, low=98, high=100)
        _write_order(store, "short-hold", "AAPL", short,
                     quantity=10, fill_qty=10, fill_price=100, status="FILLED")
        snapshot = _snapshot([_position("AAPL", -10, 100, current_price=False)])
        result = _evaluate(store, snapshot, _payload("MSFT"), proposed=10000)
        assert result.approved
        assert result.details["existing_position_risk"] == 100

    def test_broker_position_without_durable_active_stop_fails_closed(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(Exception, match="broker/ledger active position mismatch"):
            _evaluate(store, _snapshot([_position("AAPL", 10, 100)]), _payload("AAPL"))

    def test_unowned_live_broker_order_fails_closed_without_guessing_its_stop(self, tmp_path):
        store = self._store(tmp_path)
        unknown = BrokerOrder(
            broker_order_id="manual-order", client_order_id="manual-ticket",
            symbol="AAPL", side="buy", status="new", qty=10, filled_qty=0,
            filled_avg_price=None, updated_at=datetime.now(timezone.utc),
        )
        with pytest.raises(Exception, match="unowned live broker order"):
            _evaluate(store, _snapshot(orders=[unknown]), _payload("AAPL"))

    def test_pending_opening_reserves_risk_but_terminal_orders_do_not(self, tmp_path):
        store = self._store(tmp_path)
        pending = _write_order(store, "pending-open", "AAPL", _payload("AAPL"), quantity=480)
        result = _evaluate(store, _snapshot(), _payload("AAPL"), proposed=10000)
        assert result.approved and result.notional == 2000
        assert result.details["reserved_pending_risk"] == 4800

        for status in ("CANCELED", "REJECTED", "EXPIRED"):
            terminal_store = self._store(tmp_path / status.lower())
            row = _write_order(terminal_store, f"terminal-{status}", "AAPL",
                               _payload("AAPL"), quantity=480, status=status)
            decision = _evaluate(terminal_store, _snapshot(), _payload("AAPL"), proposed=10000)
            assert decision.approved and decision.notional == 10000
            assert decision.details["reserved_pending_risk"] == 0

    def test_partial_fill_is_counted_as_active_risk_plus_only_unfilled_reservation(self, tmp_path):
        store = self._store(tmp_path)
        local = _write_order(store, "partial-open", "AAPL", _payload("AAPL"),
                             quantity=200, fill_qty=100, fill_price=100)
        store.sync_order_from_broker(
            local["order_id"], "PARTIAL", broker_order_id="partial-broker", filled_qty=100,
        )
        order = BrokerOrder(
            broker_order_id="partial-broker", client_order_id=local["client_order_id"],
            symbol="AAPL", side="buy", status="partially_filled", qty=200,
            filled_qty=100, filled_avg_price=100,
            updated_at=datetime.now(timezone.utc),
        )
        result = _evaluate(
            store, _snapshot([_position("AAPL", 100, 100)], [order]),
            _payload("AAPL"), proposed=10000,
        )
        assert result.approved
        assert result.details["existing_position_risk"] == 1000
        assert result.details["reserved_pending_risk"] == 1000

    def test_unproven_partial_fill_reserves_the_full_durable_opening_size(self, tmp_path):
        store = self._store(tmp_path)
        local = _write_order(store, "unproven-partial", "AAPL", _payload("AAPL"),
                             quantity=200, fill_qty=100, fill_price=100)
        store.sync_order_from_broker(
            local["order_id"], "PARTIAL", broker_order_id="unproven-broker", filled_qty=0,
        )
        order = BrokerOrder(
            broker_order_id="unproven-broker", client_order_id=local["client_order_id"],
            symbol="AAPL", side="buy", status="partially_filled", qty=200,
            filled_qty=0, filled_avg_price=None,
            updated_at=datetime.now(timezone.utc),
        )
        result = _evaluate(
            store, _snapshot([_position("AAPL", 100, 100)], [order]),
            _payload("AAPL"), proposed=10000,
        )
        assert result.approved
        assert result.details["existing_position_risk"] == 1000
        assert result.details["reserved_pending_risk"] == 2000

    def test_protective_children_are_not_reserved_as_new_opening_risk(self, tmp_path):
        store = self._store(tmp_path)
        parent = _write_order(store, "protected-hold", "AAPL", _payload("AAPL"),
                              quantity=10, fill_qty=10, fill_price=100, status="FILLED")
        store.sync_order_from_broker(parent["order_id"], "FILLED",
                                     broker_order_id="parent-broker", filled_qty=10)
        child = SimpleNamespace(
            broker_order_id="child-broker", client_order_id="protective-child",
            symbol="AAPL", side="sell", qty=10,
        )
        store.register_protective_child(store.get_order(parent["order_id"]), child)
        broker_child = BrokerOrder(
            broker_order_id="child-broker", client_order_id="protective-child",
            symbol="AAPL", side="sell", status="new", qty=10, filled_qty=0,
            filled_avg_price=None, updated_at=datetime.now(timezone.utc), order_type="stop",
        )
        result = _evaluate(
            store, _snapshot([_position("AAPL", 10, 100)], [broker_child]),
            _payload("MSFT"), proposed=10000,
        )
        assert result.approved
        assert result.details["existing_position_risk"] == 100
        assert result.details["reserved_pending_risk"] == 0

    def test_opening_caps_keep_bracket_sibling_gross_exposure_without_double_counting_stop_risk(self, tmp_path):
        store = self._store(tmp_path)
        parent = _write_order(
            store, "bracket-held", "AAPL", _payload("AAPL"),
            quantity=9, fill_qty=9, fill_price=100, status="FILLED",
        )
        store.sync_order_from_broker(
            parent["order_id"], "FILLED", broker_order_id="parent-broker", filled_qty=9,
        )
        stop_child = SimpleNamespace(
            broker_order_id="stop-broker", client_order_id="stop-client",
            symbol="AAPL", side="sell", qty=9,
        )
        target_child = SimpleNamespace(
            broker_order_id="target-broker", client_order_id="target-client",
            symbol="AAPL", side="sell", qty=9,
        )
        parent_row = store.get_order(parent["order_id"])
        store.register_protective_child(parent_row, stop_child)
        store.register_protective_child(parent_row, target_child)
        broker_children = [
            BrokerOrder(
                broker_order_id=child.broker_order_id,
                client_order_id=child.client_order_id,
                symbol="AAPL", side="sell", status="new", qty=9, filled_qty=0,
                filled_avg_price=None, updated_at=datetime.now(timezone.utc),
                order_type=order_type,
            )
            for child, order_type in ((stop_child, "stop"), (target_child, "limit"))
        ]
        held_position = _position("AAPL", 9, 100)
        candidate = _payload("GOOG")

        protected_only = _evaluate(
            store,
            _snapshot([held_position], broker_children),
            candidate,
            proposed=10000,
        )
        assert protected_only.approved
        # Through the production cap bridge, the first SELL closes the held
        # lot and the OCO sibling remains conservatively counted as $900.
        assert protected_only.details["gross_outstanding"] == 900
        # These children protect the existing lot; the lot's stop risk is
        # already represented by existing_position_risk.
        assert protected_only.details["existing_position_risk"] == 90
        assert protected_only.details["reserved_pending_risk"] == 0

        pending = _write_order(
            store, "pending-open", "MSFT", _payload("MSFT"), quantity=10,
        )
        pending_broker_order = BrokerOrder(
            broker_order_id="pending-broker", client_order_id=pending["client_order_id"],
            symbol="MSFT", side="buy", status="new", qty=10, filled_qty=0,
            filled_avg_price=None, updated_at=datetime.now(timezone.utc),
        )
        with_pending_open = _evaluate(
            store,
            _snapshot([held_position], [*broker_children, pending_broker_order]),
            candidate,
            proposed=10000,
        )
        assert with_pending_open.approved
        assert with_pending_open.details["gross_outstanding"] == 1900
        assert with_pending_open.details["reserved_pending_risk"] == 100

    def test_recovery_uses_fresh_snapshot_risk_headroom(self, tmp_path):
        # Recovery's shared cap bridge receives a fresh snapshot. A mark rise
        # that consumes the full risk budget blocks the durable pending order.
        class Broker:
            def __init__(self):
                self.orders = []
                self.submits = []

            def get_account(self):
                return SimpleNamespace(id="paper-risk", equity="100000", last_equity="100000",
                                       cash="90000", buying_power="100000")

            def get_clock(self):
                return SimpleNamespace(is_open=True, timestamp=datetime.now(timezone.utc))

            def get_all_positions(self):
                return [SimpleNamespace(symbol="AAPL", qty="10", market_value="5900",
                                         current_price="590", avg_entry_price="100",
                                         unrealized_pl="4900")]

            def get_orders(self, request=None):
                return list(self.orders)

            def get_order_by_client_order_id(self, client_order_id):
                return next((order for order in self.orders
                             if order.client_order_id == client_order_id), None)

            def submit_order(self, request):
                self.submits.append(request)
                raise AssertionError("risk budget must block before POST")

        broker = Broker()
        db_path = tmp_path / "recovery.sqlite3"
        store = ExecutionStore(db_path)
        store.ensure_account_binding("paper-risk")
        held = _write_order(store, "recovery-held", "AAPL", _payload("AAPL"),
                            quantity=10, fill_qty=10, fill_price=100, status="FILLED")
        store.sync_order_from_broker(held["order_id"], "FILLED",
                                     broker_order_id="held-parent", filled_qty=10)
        child = SimpleNamespace(
            broker_order_id="held-stop", client_order_id="held-stop-client",
            symbol="AAPL", side="sell", qty=10,
        )
        store.register_protective_child(store.get_order(held["order_id"]), child)
        broker.orders.append(SimpleNamespace(
            id="held-stop", client_order_id="held-stop-client", symbol="AAPL",
            side="sell", status="new", qty="10", filled_qty="0",
            filled_avg_price=None, updated_at=datetime.now(timezone.utc), type="stop",
        ))
        candidate = _payload("AAPL")
        candidate_row = _write_order(store, "recovery-candidate", "AAPL", candidate,
                                     notional=1000)
        service = ExecutionService(
            db_path=db_path, broker_factory=lambda: broker,
            quote_factory=lambda symbol: BrokerQuote(
                symbol, 99.9, 100.1, datetime.now(timezone.utc),
            ),
        )
        service._quarantine_rejection = lambda symbol: None
        with patch("tradingagents.execution.service._get_execution_config", return_value={
            "max_symbol_concentration_pct": 100.0, "portfolio_max_gross_exposure_pct": 100.0,
            "portfolio_max_stop_risk_pct": 5.0, "sector_mapping": {},
        }), patch("tradingagents.screening.gate.check_entry_allowed", return_value=None), \
             patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(enabled=False)):
            result = service.startup_recover()
        assert not result["success"]
        assert any("no remaining headroom" in reason for reason in result["reconciliation_reasons"])
        assert broker.submits == []
        assert service.store.get_order(candidate_row["order_id"])["status"] == "CANCELED"

    def test_verified_risk_reducing_exit_bypasses_stop_risk_budget(self, tmp_path):
        class ExitBroker:
            def __init__(self):
                self.qty = 10
                self.submits = []

            def get_account(self):
                return SimpleNamespace(id="paper-risk", equity="100000", last_equity="100000",
                                       cash="90000", buying_power="100000")

            def get_clock(self):
                return SimpleNamespace(is_open=True, timestamp=datetime.now(timezone.utc))

            def get_all_positions(self):
                return ([SimpleNamespace(symbol="AAPL", qty=str(self.qty),
                                         market_value=str(self.qty * 100), current_price="100")]
                        if self.qty else [])

            def get_orders(self, request=None):
                return []

            def submit_order(self, request):
                self.submits.append(request)
                self.qty = 0
                return SimpleNamespace(
                    id="exit-order", client_order_id=request.client_order_id,
                    symbol="AAPL", side="sell", status="filled", qty="10",
                    filled_qty="10", filled_avg_price="100",
                    updated_at=datetime.now(timezone.utc), legs=[], notional=None,
                )

        from tradingagents.execution import service as service_module

        broker = ExitBroker()
        service = ExecutionService(
            db_path=tmp_path / "exit.sqlite3", broker_factory=lambda: broker,
        )
        with patch("tradingagents.safety.get_safety_guard", return_value=SimpleNamespace(
            enabled=False, check_order=lambda *args, **kwargs: None,
            record_order_result=lambda success: None,
        )), patch.object(
            service_module, "_evaluate_opening_caps",
            side_effect=AssertionError("reducing exit must bypass opening risk budget"),
        ):
            result = service.liquidate("AAPL")
        assert result["success"]
        assert len(broker.submits) == 1
