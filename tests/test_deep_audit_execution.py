"""Adversarial broker facts, independent of the happy-path fake broker."""
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_execution_safety_plan_a import env, now, opening  # noqa: F401 (pytest fixture)
from tradingagents.execution.authority import (
    BrokerAuthorityError, BrokerFill, BrokerOrder, BrokerQuote, BrokerSnapshot,
    Reconciler, capture_broker_snapshot, validate_quote,
)


def pending(service, **overrides):
    spec = dict(client_order_id="audit-client", symbol="AAPL", side="buy", quantity=10)
    spec.update(overrides)
    _, rows, _ = service.store.create_outbox(
        decision_id="audit-decision", run_id=None, symbol="AAPL", action="BUY",
        target_position="LONG", payload_json="{}", orders=[spec],
    )
    return rows[0]


def snapshot(*, orders=(), fills=()):
    return BrokerSnapshot(now(), "audit-version", "audit-account", 100000, 100000,
                          100000, 100000, (), tuple(orders), tuple(fills), 0)


def test_malformed_open_order_cannot_disappear_from_authority(env):
    broker = env[1]
    broker.orders = [NS(client_order_id="unknown", symbol="AAPL", status="new", side="buy")]
    with pytest.raises(BrokerAuthorityError):
        capture_broker_snapshot(broker)


@pytest.mark.parametrize("changes", [dict(side="garbage"), dict(qty=1, filled_qty=2)])
def test_impossible_order_facts_fail_capture(env, changes):
    raw = dict(id="broker-1", client_order_id="client-1", symbol="AAPL", side="buy",
               status="new", qty=10, filled_qty=0, filled_avg_price=100, updated_at=now())
    raw.update(changes)
    env[1].orders = [NS(**raw)]
    with pytest.raises(BrokerAuthorityError):
        capture_broker_snapshot(env[1])


def test_duplicate_position_symbol_is_not_a_valid_snapshot(env):
    env[1].get_all_positions = lambda: [NS(symbol="AAPL", qty=1, market_value=100)] * 2
    with pytest.raises(BrokerAuthorityError):
        capture_broker_snapshot(env[1])


@pytest.mark.parametrize("bid,ask", [(float("inf"), 100), (100, float("nan")),
                                     (101, 100), (-1, 100), (True, 100)])
def test_typed_quote_is_not_itself_proof_of_valid_prices(bid, ask):
    with pytest.raises(BrokerAuthorityError):
        validate_quote(BrokerQuote("AAPL", bid, ask, now()), "AAPL")


@pytest.mark.parametrize("field,value", [("symbol", "MSFT"), ("side", "sell"),
                                         ("broker_order_id", "different-id")])
def test_conflicting_order_cannot_mutate_ledger_or_inject_fill(env, field, value):
    service = env[0]
    row = pending(service)
    service.store.transition_order(row["order_id"], "SUBMITTING")
    service.store.transition_order(row["order_id"], "ACCEPTED", broker_order_id="broker-1")
    order = BrokerOrder("broker-1", row["client_order_id"], "AAPL", "buy", "filled",
                        10, 10, 100, now())
    order = replace(order, **{field: value})
    fill = BrokerFill("wrong-fill", order.broker_order_id, order.client_order_id, 10, 100, now())
    result = Reconciler(service.store).reconcile(snapshot(orders=[order], fills=[fill]))
    assert not result.clean
    stored = service.store.get_order(row["order_id"])
    assert stored["status"] == "ACCEPTED"
    assert stored["broker_order_id"] == "broker-1"
    assert stored["filled_qty"] == 0
    assert service.store.recorded_fill_cost(row["order_id"]) == 0


def test_duplicate_client_orders_cannot_inject_fills(env):
    service = env[0]
    row = pending(service)
    order = BrokerOrder("one", row["client_order_id"], "AAPL", "buy", "filled", 10, 10, 100, now())
    fill = BrokerFill("one:10", "one", row["client_order_id"], 10, 100, now())
    result = Reconciler(service.store).reconcile(snapshot(
        orders=[order, replace(order, broker_order_id="two")], fills=[fill]))
    assert not result.clean
    assert service.store.recorded_fill_cost(row["order_id"]) == 0


def test_mismatched_fill_cannot_change_order_status_before_rejection(env):
    service = env[0]
    row = pending(service)
    order = BrokerOrder("one", row["client_order_id"], "AAPL", "buy", "filled", 10, 10, 100, now())
    fill = BrokerFill("other:10", "other", row["client_order_id"], 10, 100, now())
    result = Reconciler(service.store).reconcile(snapshot(orders=[order], fills=[fill]))
    assert not result.clean
    assert service.store.get_order(row["order_id"])["status"] == "PENDING"


@pytest.mark.parametrize("exc", [RuntimeError("proxy failed request 40499"),
                                RuntimeError("HTTP 404 in upstream diagnostic")])
def test_lookup_text_never_proves_order_absence(env, exc):
    broker = NS(get_order_by_client_id=Mock(side_effect=exc))
    with pytest.raises(BrokerAuthorityError, match="uncertain"):
        env[0]._lookup_for_recovery(broker, "client-1")


def test_wrapped_structured_not_found_remains_supported(env):
    cause = RuntimeError("not found")
    cause.response = NS(status_code=404)
    exc = RuntimeError("wrapped")
    exc.__cause__ = cause
    assert env[0]._lookup_for_recovery(NS(get_order_by_client_id=Mock(side_effect=exc)), "id") is None


@pytest.mark.parametrize("status", ["rejected", "canceled", "expired", None])
@pytest.mark.parametrize("close", [False, True])
def test_post_failure_status_is_never_success(env, status, close):
    service, _ = env
    broker = NS(submit_order=lambda request: NS(id="response-1", status=status))
    if close:
        result = service._liquidate_core("AAPL", _broker=broker, _quantity=10, _side="sell")
        assert not result["success"], result
    else:
        row = pending(service)
        service.store.transition_order(row["order_id"], "SUBMITTING")
        result = service._submit_one(
            order_row=row, spec={"role": "close", "side": "sell", "quantity": 10},
            symbol="AAPL", intent_dict={}, broker=broker,
        )
        assert not result["ok"], result
    assert result["broker_calls"] == 1
    assert result["status"] == ("UNKNOWN" if status is None else status.upper())


@pytest.mark.parametrize("guard_failure", [False, True])
def test_protection_delete_respects_disabled_guard_kill_and_guard_failure(env, monkeypatch, guard_failure):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    current = capture_broker_snapshot(broker)
    stop = next(order for order in current.orders if order.order_type == "stop")

    def guard():
        if guard_failure:
            raise OSError("safety state unreadable")
        return NS(enabled=False, kill_switch_active=lambda: True)

    monkeypatch.setattr("tradingagents.safety.get_safety_guard", guard)
    try:
        service._cancel_protection_with_race_check(broker, current, stop, account_id=current.account_id)
    except BrokerAuthorityError:
        pass
    assert broker.cancels == []


@pytest.mark.parametrize("slow_step", ["commit", "request"])
def test_recovery_rechecks_freshness_after_local_blocking_work(env, monkeypatch, slow_step):
    import tradingagents.execution.authority as authority
    import tradingagents.execution.service as entry

    service, broker = env
    intent = opening()
    service.store.ensure_account_binding(broker.get_account().id)
    _, rows, _ = service.store.create_outbox(
        decision_id="recovery-slow", run_id=None, symbol="AAPL", action="BUY",
        target_position="LONG", payload_json=json.dumps(intent),
        orders=[dict(client_order_id="recovery-slow", symbol="AAPL", side="buy", quantity=10)],
    )
    current = capture_broker_snapshot(broker)
    if slow_step == "commit":
        original = service.store.transition_order

        def transition(order_id, status, **kwargs):
            result = original(order_id, status, **kwargs)
            if status == "SUBMITTING":
                monkeypatch.setattr(authority, "utc_now", lambda: current.observed_at + timedelta(minutes=2))
            return result

        monkeypatch.setattr(service.store, "transition_order", transition)
    else:
        original = entry._build_protective_request

        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            monkeypatch.setattr(authority, "utc_now", lambda: current.observed_at + timedelta(minutes=2))
            return result

        monkeypatch.setattr(entry, "_build_protective_request", build)
    with pytest.raises(BrokerAuthorityError, match="stale"):
        service._resubmit_recovered(broker, rows[0], current)
    assert broker.submits == []
