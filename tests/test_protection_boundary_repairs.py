"""Offline protection coverage and mutation-order regressions."""

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
