"""Offline protection coverage and mutation-order regressions."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_execution_safety_plan_a import (
    enable_broker_shorting_for_test, env, opening,
)


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
@pytest.mark.parametrize("stop_status", ["canceled", "rejected", "expired"])
def test_take_profit_alone_does_not_cover_position(env, action, stop_status):
    service, broker = env
    if action == "SHORT":
        enable_broker_shorting_for_test(broker)
    assert service.execute(
        trade_intent=opening(action), dollar_amount=1000, allow_shorts=True
    )["success"]
    stop, target = broker.orders[0].legs
    stop.type = "stop"
    target.type = "limit"
    stop.status = stop_status
    submits = len(broker.submits)
    result = service.startup_recover()
    assert not result["success"], result
    assert "PROTECTION_GAP" in str(result)
    assert len(broker.submits) == submits
    assert broker.cancels == []


def test_after_hours_exit_keeps_existing_stop(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    stop = broker.orders[0].legs[0]
    broker.get_clock = lambda: SimpleNamespace(
        is_open=False, timestamp=datetime.now(timezone.utc)
    )
    submits = len(broker.submits)

    result = service.liquidate("AAPL")

    assert not result["success"], result
    assert broker.cancels == []
    assert len(broker.submits) == submits
    assert stop.status == "new"


def test_session_closes_before_delete_keeps_existing_stop(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    stop = broker.orders[0].legs[0]
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            is_open=calls == 1, timestamp=datetime.now(timezone.utc)
        )

    broker.get_clock = clock
    submits = len(broker.submits)
    result = service.liquidate("AAPL")

    assert not result["success"], result
    assert calls >= 2
    assert broker.cancels == []
    assert len(broker.submits) == submits
    assert stop.status == "new"


def test_kill_engaged_during_final_cancel_clock_get_keeps_protections(env, monkeypatch):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    stop, target = broker.orders[0].legs
    guard = SimpleNamespace(enabled=True, active=False,
                            kill_switch_active=lambda: guard.active,
                            check_order=lambda *a, **kw: SimpleNamespace(allowed=True, reasons=[]),
                            status=lambda: {})
    monkeypatch.setattr("tradingagents.safety.get_safety_guard", lambda: guard)

    def clock():
        guard.active = True
        return SimpleNamespace(is_open=True, timestamp=datetime.now(timezone.utc))

    broker.get_clock = clock
    submits = len(broker.submits)
    result = service.liquidate("AAPL")
    assert not result["success"]
    assert broker.cancels == []
    assert len(broker.submits) == submits
    assert stop.status == target.status == "new"
    assert broker.qty == 9


def test_pending_full_close_recovers_after_protection_cancel_crash(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    prepared = service._prepare_liquidation_outbox(
        "AAPL", decision_id="crash-close", quantity=9, side="sell")
    assert prepared["ok"]
    for leg in broker.orders[0].legs:
        leg.status = "canceled"
    before = len(broker.submits)
    result = service.startup_recover()
    assert result["success"], result
    assert len(broker.submits) == before + 1
    assert broker.submits[-1].client_order_id == prepared["client_order_id"]
    assert broker.qty == 0
    again = service.startup_recover()
    assert again["success"], again
    assert len(broker.submits) == before + 1


def test_protective_fill_consumes_its_parent_lot_and_keeps_earlier_deadline(env):
    import json
    from tradingagents.execution.lifecycle import due_positions, remaining_lots
    service, _broker = env
    store = service.store
    parents = []
    for number, deadline in ((1, "2026-09-28T00:00:00+00:00"),
                             (2, "2026-10-05T00:00:00+00:00")):
        _, rows, _ = store.create_outbox(
            decision_id=f"lot-{number}", run_id=None, symbol="AAPL",
            action="BUY", target_position="LONG",
            payload_json=json.dumps({"entry_policy": {"exit_by": deadline}}),
            orders=[{"client_order_id": f"lot-{number}-open", "symbol": "AAPL",
                     "side": "buy", "quantity": 5, "notional": None}],
        )
        parent = rows[0]
        store.record_fill(execution_id=f"lot-{number}-fill", order_id=parent["order_id"],
                          qty=5, price=100, filled_at=f"2026-09-26T00:00:0{number}+00:00")
        parents.append(parent)
    child = SimpleNamespace(client_order_id="lot-2-target", broker_order_id="broker-target-2",
                            symbol="AAPL", side="sell", qty=5)
    store.register_protective_child(parents[1], child)
    from tradingagents.execution.store import order_id_for_client
    store.record_fill(execution_id="lot-2-target-fill",
                      order_id=order_id_for_client(child.client_order_id), qty=5, price=110,
                      filled_at="2026-09-26T00:00:03+00:00")
    lots = remaining_lots(store)["AAPL"]
    assert len(lots) == 1
    assert lots[0]["order"]["order_id"] == parents[0]["order_id"]
    assert due_positions(store, datetime(2026, 9, 29, tzinfo=timezone.utc))["AAPL"]["qty"] == 5


def test_canonical_stop_must_stay_outside_entry_range():
    from tradingagents.execution.policy import worst_case_risk_reward
    assert worst_case_risk_reward(
        target_position="LONG", minimum_price=100.00, maximum_price=100.01,
        stop_loss_price=99.995, take_profit_price=100.04,
    ) is None


def test_session_closes_after_delete_pauses_without_queued_close(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    original_cancel = broker.cancel_order_by_id
    session_open = True

    def cancel_then_close(order_id):
        nonlocal session_open
        original_cancel(order_id)
        session_open = False

    broker.cancel_order_by_id = cancel_then_close
    broker.get_clock = lambda: SimpleNamespace(
        is_open=session_open, timestamp=datetime.now(timezone.utc)
    )
    submits = len(broker.submits)

    result = service.liquidate("AAPL")

    assert not result["success"], result
    assert len(broker.cancels) == 1
    assert len(broker.submits) == submits
    assert result["broker_calls"] == 1
    assert result["broker_attempted"] is True
    assert service.store.get_account_state("audit-fixture")["state"] == "PAUSED"
