import pytest

from tradingagents.execution.order_planning import _planned_order_specs


def _intent(*actions):
    return {"planned_actions": list(actions), "order_intent": {}}


def test_explicit_side_is_kept():
    specs = _planned_order_specs(_intent(
        {"action": "close_short", "order_type": "close_position", "side": "BUY"}), None)
    assert [(s["role"], s["side"]) for s in specs] == [("close", "buy")]


def test_missing_side_comes_from_the_canonical_action_not_the_role():
    # Closing a short is a BUY; the old role-based guess made every close a
    # SELL and, through a dead branch, every missing side a BUY.
    specs = _planned_order_specs(_intent(
        {"action": "close_short", "order_type": "close_position"},
        {"action": "open_long", "order_type": "market"}), 500.0)
    assert [(s["role"], s["side"]) for s in specs] == [("close", "buy"), ("open", "buy")]
    specs = _planned_order_specs(_intent(
        {"action": "close_long", "order_type": "close_position"},
        {"action": "open_short", "order_type": "market"}), 500.0)
    assert [(s["role"], s["side"]) for s in specs] == [("close", "sell"), ("open", "sell")]


def test_an_underivable_side_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="broker side"):
        _planned_order_specs(_intent({"action": "rebalance", "order_type": "market"}), 500.0)
