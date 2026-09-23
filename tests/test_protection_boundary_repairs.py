"""Offline protection coverage and mutation-order regressions."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_execution_safety_plan_a import env, opening


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
@pytest.mark.parametrize("stop_status", ["canceled", "rejected", "expired"])
def test_take_profit_alone_does_not_cover_position(env, action, stop_status):
    service, broker = env
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
