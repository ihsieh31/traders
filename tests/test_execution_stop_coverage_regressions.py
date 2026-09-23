"""Offline stop-proof, recovery ordering, and prohibited reversal regressions."""
import json
from enum import Enum
from types import SimpleNamespace as NS

import pytest

from test_execution_safety_plan_a import (
    enable_broker_shorting_for_test, env, opening, now,
)
from test_full_review_phase1_regressions import _buy_intent
from tradingagents.execution.authority import capture_broker_snapshot
from tradingagents.execution.store import canonical_decision_id, client_order_id_for


@pytest.fixture
def protected(env):
    service, broker = env
    submit = broker.submit_order

    def typed_submit(request):
        parent = submit(request)
        parent.type = request.type
        for child in parent.legs:
            child.type = "stop" if child.id.startswith("stop-") else "limit"
        return parent

    broker.submit_order = typed_submit
    return service, broker


def enter(protected, short=False):
    service, broker = protected
    if short:
        enable_broker_shorting_for_test(broker)
    result = service.execute(trade_intent=opening("SHORT" if short else "BUY"),
                             dollar_amount=1000, allow_shorts=short)
    assert result["success"], result
    assert not result.get("paused"), result
    return broker.orders[0].legs


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("case", ["terminal_stop", "insufficient_stop", "unknown", "missing",
                                  "stop", "stop_limit", "trailing_stop"])
def test_only_proven_stops_cover_position(protected, short, case):
    service, broker = protected
    stop, target = enter(protected, short)
    if case == "terminal_stop":
        stop.status = "canceled"
    elif case == "insufficient_stop":
        stop.qty = 1
    elif case == "missing":
        del stop.type
    else:
        stop.type = case
    snapshot = capture_broker_snapshot(broker)
    gaps = service._protection_coverage_gaps(snapshot)
    expected_gap = case in {"terminal_stop", "insufficient_stop", "unknown", "missing"}
    assert bool(gaps) == expected_gap, gaps
    covered = service._program_owned_live_reducing_qty(snapshot, "AAPL", "buy" if short else "sell")
    assert covered == (1 if case == "insufficient_stop" else 0 if expected_gap else 9)


def test_persisted_gap_not_cleared_by_take_profit(protected):
    service, broker = protected
    stop, _ = enter(protected)
    stop.status = "canceled"
    snapshot = capture_broker_snapshot(broker)
    reason = "PROTECTION_GAP: AAPL previously lost stop protection"
    service.store.save_account_state(account_id=snapshot.account_id, state="PAUSED",
                                    reasons=(reason,), snapshot_version=snapshot.version,
                                    baseline_positions={"AAPL": broker.qty})
    result = service._reconcile_snapshot(broker, snapshot)
    assert not result.clean
    assert reason in result.reasons


def test_live_standalone_close_covers_without_stops(protected):
    service, broker = protected
    legs = enter(protected)
    for child in legs:
        child.status = "canceled"
    outbox = service._prepare_liquidation_outbox("AAPL", decision_id="standalone-close", quantity=9, side="sell")
    row = outbox["order_rows"][0]
    close = NS(id="live-close", client_order_id=row["client_order_id"], symbol="AAPL", side="sell",
               type="market", status="accepted", qty=9, filled_qty=0, filled_avg_price=None,
               updated_at=now(), legs=[])
    broker.orders.append(close)
    snapshot = capture_broker_snapshot(broker)
    service._reconcile_snapshot(broker, snapshot)
    assert service._protection_coverage_gaps(snapshot) == []
    assert service._program_owned_live_reducing_qty(snapshot, "AAPL", "sell") == 9


def test_relation_lookup_failure_never_promotes_target_to_close(protected, monkeypatch):
    service, broker = protected
    stop, _ = enter(protected)
    stop.status = "canceled"
    snapshot = capture_broker_snapshot(broker)
    def unavailable(_):
        raise RuntimeError("relation ledger unavailable")
    monkeypatch.setattr(service.store, "protective_parent", unavailable)
    assert service._program_owned_live_reducing_qty(snapshot, "AAPL", "sell") == 0
    assert service._protection_coverage_gaps(snapshot)


def test_e01_canceled_parent_with_unspent_lots_still_owes_protection(env):
    """E01: a canceled/expired opening parent whose durable fills still show
    unspent shares owes protection. The old N07 fallback only trusted a
    FILLED parent row, so a canceled-partial ledger row made
    _protection_coverage_gaps return [] and left the broker position bare.
    Seeded WITHOUT any protective child relation so the intent fallback is
    the only possible proof path."""
    service, broker = env
    from tradingagents.execution.lifecycle import remaining_lots
    did = "e01-canceled-partial"
    coid = client_order_id_for(did, "AAPL", "buy", role="open", seq=0)
    payload = json.dumps({
        "symbol": "AAPL", "action": "BUY", "target_position": "LONG",
        "risk_controls": {"stop_loss_price": 90},
        "entry_policy": {"exit_by": None},
    })
    _, rows, _ = service.store.create_outbox(
        decision_id=did, run_id=None, symbol="AAPL", action="BUY",
        target_position="LONG", payload_json=payload,
        orders=[dict(client_order_id=coid, symbol="AAPL", side="buy",
                     quantity=9, notional=None)],
    )
    row = rows[0]
    service.store.sync_order_from_broker(row["order_id"], "FILLED",
                                         broker_order_id="e01-parent", filled_qty=9)
    service.store.record_fill(execution_id="e01-fill", order_id=row["order_id"],
                              qty=9, price=100.0)
    service.store.sync_order_from_broker(row["order_id"], "CANCELED",
                                         broker_order_id="e01-parent", filled_qty=9)
    assert service.store.get_order(row["order_id"])["status"] == "CANCELED"
    assert remaining_lots(service.store).get("AAPL"), "9 unspent shares are provable"
    # Broker: live 9-share long; the protective child never appeared and no
    # reducing order exists.
    broker.qty = 9
    broker.orders = []
    snapshot = capture_broker_snapshot(broker)
    gaps = service._protection_coverage_gaps(snapshot)
    assert gaps and any("AAPL" in g for g in gaps)


def test_e01_fully_sold_out_lots_never_gap_manual_position(env):
    """E01 guard: a long-ago, fully sold-out program entry must not force
    protection onto a later manual position — unspent durable lots, not the
    historic FILLED row, prove the obligation."""
    service, broker = env
    from tradingagents.execution.lifecycle import remaining_lots
    _, rows, _ = service.store.create_outbox(
        decision_id="e01-history-entry", run_id=None, symbol="AAPL",
        action="BUY", target_position="LONG",
        payload_json=json.dumps({
            "symbol": "AAPL", "action": "BUY", "target_position": "LONG",
            "risk_controls": {"stop_loss_price": 90},
            "entry_policy": {"exit_by": None},
        }),
        orders=[dict(client_order_id=client_order_id_for("e01-history-entry", "AAPL", "buy", role="open", seq=0),
                     symbol="AAPL", side="buy", quantity=9, notional=None)],
    )
    buy_row = rows[0]
    service.store.sync_order_from_broker(buy_row["order_id"], "FILLED",
                                         broker_order_id="e01-old-parent", filled_qty=9)
    service.store.record_fill(execution_id="e01-buy", order_id=buy_row["order_id"],
                              qty=9, price=100.0)
    _, rows2, _ = service.store.create_outbox(
        decision_id="e01-history-close", run_id=None, symbol="AAPL",
        action="SELL", target_position="NEUTRAL",
        payload_json=json.dumps({"symbol": "AAPL", "action": "SELL", "kind": "liquidation"}),
        orders=[dict(client_order_id=client_order_id_for("e01-history-close", "AAPL", "sell", role="close", seq=0),
                     symbol="AAPL", side="sell", quantity=9, notional=None)],
    )
    sell_row = rows2[0]
    service.store.sync_order_from_broker(sell_row["order_id"], "FILLED",
                                         broker_order_id="e01-old-close", filled_qty=9)
    service.store.record_fill(execution_id="e01-sell", order_id=sell_row["order_id"],
                              qty=9, price=101.0)
    assert not remaining_lots(service.store).get("AAPL"), "the lot was fully sold back out"
    # A later MANUAL 4-share long appears; no program orders are live.
    broker.qty = 4
    broker.orders = []
    snapshot = capture_broker_snapshot(broker)
    assert service._protection_coverage_gaps(snapshot) == []


def test_e04_accepted_row_adopts_broker_terminal_fact_and_cleans(env):
    """E04: a durable ACCEPTED row whose order is missing from the broker
    snapshot must enter the read-only client-ID lookup; a broker terminal
    fact (canceled) is adopted and the account recovers to CLEAN with zero
    POSTs. On HEAD the row is invisible to recovery and the account stays
    PAUSED forever (F-02)."""
    service, broker = env
    service.store.ensure_account_binding("audit-fixture")
    prepared = service._prepare_liquidation_outbox("AAPL", decision_id="e04-accepted", quantity=4)
    row = prepared["order_rows"][0]
    service.store.sync_order_from_broker(row["order_id"], "ACCEPTED",
                                         broker_order_id="e04-oid", filled_qty=0)
    canceled = NS(id="e04-oid", client_order_id=row["client_order_id"], symbol="AAPL",
                  side="sell", type="market", status="canceled", qty=4, filled_qty=0,
                  filled_avg_price=None, updated_at=now(), legs=[], notional=None)
    # Order absent from the listing window, but findable by client ID.
    broker.orders = []
    broker.get_order_by_client_id = lambda cid: canceled if cid == row["client_order_id"] else None
    result = service.startup_recover()
    assert result["success"], result
    assert result["account_execution_state"] == "CLEAN"
    assert service.store.get_order(row["order_id"])["status"] == "CANCELED"
    assert broker.submits == []


def test_e04_accepted_row_not_on_broker_stays_paused_without_resubmit(env):
    """E04 guard: an ACCEPTED row with NO broker fact anywhere is never
    resubmitted — the bounded lookup fails and the account stays PAUSED."""
    service, broker = env
    prepared = service._prepare_liquidation_outbox("AAPL", decision_id="e04-ghost", quantity=4)
    row = prepared["order_rows"][0]
    service.store.sync_order_from_broker(row["order_id"], "ACCEPTED",
                                         broker_order_id="e04-ghost-oid", filled_qty=0)
    broker.orders = []
    result = service.startup_recover()
    assert not result["success"]
    assert broker.submits == []
    assert service.store.get_order(row["order_id"])["status"] == "ACCEPTED"


def seed_pending(service, symbol):
    did = "pending-" + symbol
    coid = client_order_id_for(did, symbol, "buy", role="open", seq=0)
    _, rows, _ = service.store.create_outbox(
        decision_id=did, run_id=None, symbol=symbol, action="BUY", target_position="LONG",
        payload_json=json.dumps(_buy_intent(symbol)),
        orders=[dict(client_order_id=coid, symbol=symbol, side="buy", quantity=None, notional=1000)],
    )
    return rows[0]


@pytest.mark.parametrize("between_items", [False, True])
def test_recovery_checks_gap_before_each_post(protected, between_items):
    service, broker = protected
    stop, target = enter(protected)
    # No persisted gap exists: this is newly lost protection at initial capture
    # or on the first recovery POST, before the second queue item's turn.
    first = seed_pending(service, "AAA")
    second = seed_pending(service, "BBB")
    posts = []
    def recovery_submit(request):
        posts.append(request)
        stop.status = target.status = "canceled"
        order = NS(id="recovered-" + request.symbol, client_order_id=request.client_order_id,
                   symbol=request.symbol, side="buy", type="limit", status="accepted",
                   qty=request.qty, filled_qty=0, filled_avg_price=None, updated_at=now(), legs=[])
        broker.orders.append(order)
        return order
    broker.submit_order = recovery_submit
    if not between_items:
        stop.status = target.status = "canceled"
    result = service.startup_recover()
    assert len(posts) == int(between_items), result
    assert result["account_execution_state"] == "PAUSED" and not result["success"], result
    assert any("PROTECTION_GAP:" in r for r in result["reconciliation_reasons"])
    assert service.store.get_order(second["order_id"]) == second
    if not between_items:
        assert service.store.get_order(first["order_id"]) == first


@pytest.mark.parametrize("permission", [False, None])
def test_prohibited_reversal_preserves_position_and_protection(protected, permission):
    service, broker = protected
    legs = enter(protected)
    before = [(o.id, o.status) for o in broker.orders]
    kwargs = {} if permission is None else {"allow_shorts": permission}
    result = service.execute(trade_intent=opening("SHORT", "LONG"), dollar_amount=1000, **kwargs)
    assert not result["success"]
    assert broker.cancels == []
    assert len(broker.submits) == 1
    assert broker.qty == 9
    assert [(o.id, o.status) for o in broker.orders] == before
    assert all(leg.status == "new" for leg in legs)
    assert result["broker_calls"] == 0


@pytest.mark.parametrize("status", ["PENDING", "SUBMITTING", "UNKNOWN", "ACCEPTED"])
def test_prohibited_duplicate_leaves_durable_rows_unchanged(protected, status, monkeypatch):
    service, broker = protected
    intent = opening("SHORT", "LONG")
    did = canonical_decision_id(intent)
    outbox = service._prepare_liquidation_outbox("AAPL", decision_id=did, quantity=9, side="sell")
    row = outbox["order_rows"][0]
    service.store.sync_order_from_broker(row["order_id"], status, broker_order_id="prior", filled_qty=0)
    before = service.store.get_order(row["order_id"])
    before_intent = service.store.get_intent_for_order(row["order_id"])
    # The early guard must precede even deadline/recovery GETs and mutations.
    monkeypatch.setattr(service, "enforce_exit_deadlines", lambda **kw: pytest.fail("deadline called"))
    result = service.execute(trade_intent=intent, dollar_amount=1000)
    assert not result["success"] and result["broker_calls"] == 0
    assert service.store.get_order(row["order_id"]) == before
    assert service.store.get_intent_for_order(row["order_id"]) == before_intent


def test_snapshot_normalizes_type_and_fingerprints_it(protected):
    _, broker = protected
    stop, _ = enter(protected)
    class Type(Enum):
        STOP = "stop"
    stop.type = Type.STOP
    stamp = now()
    first = capture_broker_snapshot(broker, now=lambda: stamp)
    assert next(o for o in first.orders if o.broker_order_id == stop.id).order_type == "stop"
    stop.type = "limit"
    second = capture_broker_snapshot(broker, now=lambda: stamp)
    assert first.version != second.version
