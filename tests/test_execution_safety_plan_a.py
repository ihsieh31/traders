"""Plan-A execution-safety regressions (R03/R04/R05/R06/R07/R09/R14).

Formal corrected-behavior tests derived from
``docs/paper_readiness_20260911_repros.py`` (whose assertions demonstrate
the DEFECTS). Every transport is faked in-process: no real Alpaca mutation,
no paid LLM call, no network.
"""

import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import tradingagents.agents  # noqa: F401  (production-safe import order)
from tradingagents.agents.schemas import (
    EntryPolicy, RiskDecision, build_trade_intent_from_risk_decision,
)
from tradingagents.execution.authority import (
    BrokerOrder, BrokerQuote, BrokerSnapshot,
)
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.store import client_order_id_for, canonical_decision_id
from tradingagents.risk.exposure import outstanding_increasing_notional


def now():
    return datetime.now(timezone.utc)


def opening(action="BUY", current="NEUTRAL", *, stamp=None):
    stamp = stamp or now()
    short = action == "SHORT"
    decision = RiskDecision(
        action=action, confidence="high", risk_rationale="fixture", required_controls="stop",
        stop_loss_price=110 if short else 90,
        take_profit_price=80 if short else 120,
        entry_policy=EntryPolicy(
            status="READY", minimum_price=99, maximum_price=101,
            expires_at=(stamp + timedelta(hours=1)).isoformat(),
            exit_by=(stamp + timedelta(days=5)).isoformat(), confirmation="fixture",
        ),
    )
    data = build_trade_intent_from_risk_decision(
        symbol="AAPL", trading_mode="investment" if action == "BUY" else "trading",
        current_position=current, decision=decision, allow_shorts=True,
    ).model_dump(mode="json")
    data["generated_at"] = stamp.isoformat()
    return data


class Broker:
    """Paper broker: bracket entries fill immediately; canceling one bracket
    leg cascade-cancels the sibling (Alpaca behavior)."""

    def __init__(self):
        self.qty = 0
        self.orders = []
        self.submits = []
        self.cancels = []

    def get_account(self):
        return NS(id="audit-fixture", equity=100000, last_equity=100000,
                  cash=100000, buying_power=100000)

    def get_clock(self):
        # R13: the opening gate proves the regular session from the broker's
        # own clock before any exposure-adding POST; the fixture keeps it open.
        return NS(is_open=True)

    def get_all_positions(self):
        return [NS(symbol="AAPL", qty=self.qty, market_value=self.qty * 100)] if self.qty else []

    def get_orders(self, request):
        if str(getattr(request.status, "value", request.status)) == "open":
            return [o for o in self.orders if o.status not in {"filled", "canceled", "expired", "rejected"}]
        return list(reversed(self.orders))[:500]

    def get_order_by_id(self, order_id, filter=None):
        return next(o for o in self.orders if o.id == order_id)

    def get_order_by_client_id(self, client_id):
        return next((o for o in self.orders if o.client_order_id == client_id), None)

    def submit_order(self, request):
        self.submits.append(request)
        qty = float(getattr(request, "qty", 0) or 0)
        side = str(getattr(getattr(request, "side", None), "value", getattr(request, "side", "")))
        self.qty += qty if side == "buy" else -qty
        order = NS(id=f"parent-{len(self.submits)}", client_order_id=request.client_order_id,
                   symbol="AAPL", side=side, qty=qty, filled_qty=qty, filled_avg_price=100,
                   status="filled", updated_at=now(), legs=[], notional=None)
        self.orders.append(order)
        if getattr(request, "stop_loss", None):
            for kind in (["stop", "target"] if request.take_profit else ["stop"]):
                child = NS(id=f"{kind}-{len(self.submits)}",
                           client_order_id=f"broker-{kind}-{len(self.submits)}", symbol="AAPL",
                           side="sell" if side == "buy" else "buy", qty=qty, filled_qty=0,
                           filled_avg_price=None, status="new", updated_at=now(), legs=[], notional=None)
                order.legs.append(child)
                self.orders.append(child)
        return order

    def cancel_order_by_id(self, order_id):
        self.cancels.append(order_id)
        child = self.get_order_by_id(order_id)
        if child.status == "canceled":
            raise RuntimeError("422 order is already canceled")
        # Alpaca bracket cancellation also cancels the other live child.
        parent = next((o for o in self.orders if child in o.legs), None)
        for leg in (parent.legs if parent else [child]):
            leg.status = "canceled"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tradingagents.dataflows import config as cfg
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setattr(cfg, "_config", {**DEFAULT_CONFIG, "auto_screening_enabled": False,
                                         "data_cache_dir": str(tmp_path), "alerts_enabled": False})
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network forbidden")))
    guard = NS(enabled=False, check_order=lambda *a, **k: NS(allowed=True, reasons=[]),
               check_llm_budget=lambda: NS(allowed=True, reasons=[]), status=lambda: {})
    monkeypatch.setattr("tradingagents.safety.get_safety_guard", lambda: guard)
    broker = Broker()
    service = ExecutionService(db_path=tmp_path / "execution.db", broker_factory=lambda: broker,
                               quote_factory=lambda s: BrokerQuote(s, 99.9, 100.1, now()))
    service._quarantine_rejection = lambda symbol: None
    return service, broker


def _submit_sides(broker):
    return [
        str(getattr(getattr(s, "side", None), "value", getattr(s, "side", "")))
        for s in broker.submits
    ]


def _freeze_time_after_commit(monkeypatch, service, future, *, freeze_policy_clock=False):
    """Advance the clock the moment the durable outbox commit completes (R05).

    ``authority.utc_now`` drives freshness checks; ``freeze_policy_clock``
    additionally moves the entry-policy clock (``policy.datetime``) used by
    ``entry_check`` for authorization expiry.
    """
    original = service.store.create_outbox

    def persist_then_delay(**kwargs):
        result = original(**kwargs)
        monkeypatch.setattr("tradingagents.execution.authority.utc_now", lambda: future)
        if freeze_policy_clock:
            class Later(datetime):
                @classmethod
                def now(cls, tz=None):
                    return future if tz else future.replace(tzinfo=None)

            monkeypatch.setattr("tradingagents.execution.policy.datetime", Later)
        return result

    monkeypatch.setattr(service.store, "create_outbox", persist_then_delay)


# ---------------------------------------------------------------------------
# R03 — bracket cancel cascade must not abort the close
# ---------------------------------------------------------------------------


def test_r03_bracket_cancel_cascade_still_reaches_safe_close(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    assert broker.qty == 9
    result = service.liquidate("AAPL")
    assert result["success"], result
    assert broker.qty == 0, "the position must actually be closed"
    # One DELETE (the stop) cascade-canceled the target; no second DELETE was
    # sent, and the close POST still happened.
    assert len(broker.cancels) == 1
    assert all(leg.status == "canceled" for leg in broker.orders[0].legs)
    assert _submit_sides(broker) == ["buy", "sell"]
    assert result["broker_calls"] == 2  # 1 DELETE + 1 close POST, honestly counted


def test_r03_cancel_race_unproven_failure_pauses(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]

    def refusing_cancel(order_id):
        broker.cancels.append(order_id)
        raise RuntimeError("422 cancellation refused")  # child stays live

    broker.cancel_order_by_id = refusing_cancel
    result = service.liquidate("AAPL")
    assert not result["success"] and result.get("paused"), result
    # The close was never sent: fresh facts still show the live protective
    # children, so the verified-exit gate refuses instead of racing bare.
    assert len(broker.submits) == 1
    assert broker.qty == 9
    # Both DELETE attempts (stop + target) are truthfully counted as
    # mutations even though each raised.
    assert result["broker_calls"] == 2


# ---------------------------------------------------------------------------
# R04 — a reversal executes its close phase only
# ---------------------------------------------------------------------------


def test_r04_protected_long_reversal_submits_close_only(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    assert broker.qty == 9
    reversal = opening("SHORT", "LONG")
    assert len(reversal["planned_actions"]) == 2
    result = service.execute(trade_intent=reversal, dollar_amount=1000, allow_shorts=True)
    assert result["success"], result
    # Exactly one new POST: the SELL close of the long. The long's own
    # protective orders were canceled, not treated as a conflict.
    assert _submit_sides(broker) == ["buy", "sell"]
    assert broker.qty == 0
    assert result.get("reanalysis_required") is True


def test_r04_protected_short_reversal_submits_close_only(env):
    service, broker = env
    assert service.execute(
        trade_intent=opening("SHORT", "NEUTRAL"), dollar_amount=1000, allow_shorts=True
    )["success"]
    assert broker.qty == -9
    result = service.execute(
        trade_intent=opening("LONG", "SHORT"), dollar_amount=1000, allow_shorts=True
    )
    assert result["success"], result
    # Symmetric: the SHORT's protective BUY children were canceled and the
    # close is a BUY of the held quantity — never an opening SELL.
    assert _submit_sides(broker) == ["sell", "buy"]
    assert broker.qty == 0


def test_r04_reversal_never_submits_opposite_open_same_call(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    reversal = opening("SHORT", "LONG")
    result = service.execute(trade_intent=reversal, dollar_amount=1000, allow_shorts=True)
    assert result["success"], result
    assert len(broker.submits) == 2, "only entry + close may POST in one call"
    # The durable opposite-open leg is CANCELED (deferred), never PENDING.
    did = canonical_decision_id(reversal)
    open_coid = client_order_id_for(did, "AAPL", "sell", role="open", seq=1)
    row = service.store.get_order_by_client(open_coid)
    assert row is not None and row["status"] == "CANCELED", row


# ---------------------------------------------------------------------------
# R05 — entry authorization is re-proven at the final POST boundary
# ---------------------------------------------------------------------------


def test_r05_expired_at_dispatch_zero_broker_posts(env, monkeypatch):
    service, broker = env
    monkeypatch.setenv("TRADINGAGENTS_QUOTE_TTL_SECONDS", "3600")
    monkeypatch.setenv("TRADINGAGENTS_SNAPSHOT_TTL_SECONDS", "3600")
    stamp = now()
    data = opening(stamp=stamp)
    data["entry_policy"]["expires_at"] = (stamp + timedelta(seconds=30)).isoformat()
    _freeze_time_after_commit(monkeypatch, service, stamp + timedelta(seconds=60),
                              freeze_policy_clock=True)
    result = service.execute(trade_intent=data, dollar_amount=1000)
    assert len(broker.submits) == 0, "an expired authorization must never POST"
    assert result["success"] is False
    first = result["results"][0]
    assert first["pre_submit_blocked"] and first["broker_calls"] == 0, first
    assert "expired" in first["error"]
    assert result["orders"][0]["status"] == "CANCELED"


def test_r05_stale_quote_at_dispatch_zero_broker_posts(env, monkeypatch):
    service, broker = env
    stamp = now()
    data = opening(stamp=stamp)
    # Quote TTL is 15s, snapshot TTL 30s: +20s isolates the quote staleness.
    _freeze_time_after_commit(monkeypatch, service, stamp + timedelta(seconds=20))
    result = service.execute(trade_intent=data, dollar_amount=1000)
    assert len(broker.submits) == 0
    first = result["results"][0]
    assert first["pre_submit_blocked"] and first["broker_calls"] == 0, first
    assert "quote" in first["error"]
    assert result["orders"][0]["status"] == "CANCELED"


def test_r05_stale_snapshot_at_dispatch_zero_broker_posts(env, monkeypatch):
    service, broker = env
    monkeypatch.setenv("TRADINGAGENTS_QUOTE_TTL_SECONDS", "3600")
    stamp = now()
    data = opening(stamp=stamp)
    # Snapshot TTL stays 30s while the quote tolerates an hour: +40s
    # isolates the snapshot staleness.
    _freeze_time_after_commit(monkeypatch, service, stamp + timedelta(seconds=40))
    result = service.execute(trade_intent=data, dollar_amount=1000)
    assert len(broker.submits) == 0
    first = result["results"][0]
    assert first["pre_submit_blocked"] and first["broker_calls"] == 0, first
    assert "snapshot" in first["error"]
    assert result["orders"][0]["status"] == "CANCELED"


# ---------------------------------------------------------------------------
# R06 — done_for_day stays live for exposure accounting
# ---------------------------------------------------------------------------


def test_r06_done_for_day_consumes_headroom():
    stamp = now()
    order = BrokerOrder("b", "ta-b", "AAPL", "buy", "done_for_day", 100, 0, None, stamp, None)
    snap = BrokerSnapshot(stamp, "v", "fixture", 100000, 100000, 100000, 100000, (), (order,), (), 0)
    amount, complete = outstanding_increasing_notional(snap, reference_prices={"AAPL": 100})
    assert amount == 10000 and complete


def test_r06_terminal_orders_do_not_consume_headroom():
    stamp = now()
    for status in ("filled", "canceled", "cancelled", "rejected", "expired"):
        order = BrokerOrder("b", "ta-b", "AAPL", "buy", status, 100, 0, None, stamp, None)
        snap = BrokerSnapshot(stamp, "v", "fixture", 100000, 100000, 100000, 100000, (), (order,), (), 0)
        amount, complete = outstanding_increasing_notional(snap, reference_prices={"AAPL": 100})
        assert amount == 0 and complete, status


# ---------------------------------------------------------------------------
# R07 — unprovable submit outcomes are UNKNOWN, never REJECTED
# ---------------------------------------------------------------------------


def _http_500_api_error():
    from alpaca.common.exceptions import APIError
    from requests import HTTPError, Response

    response = Response()
    response.status_code = 500
    return APIError(
        '{"code":50010000,"message":"Internal Server Error"}',
        HTTPError(response=response),
    )


def test_r07_http_500_becomes_unknown(env):
    service, broker = env
    broker.submit_order = Mock(side_effect=_http_500_api_error())
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    assert broker.submit_order.call_count == 1, "no same-call retry after a 5xx"
    assert result["orders"][0]["status"] == "UNKNOWN", result
    assert result.get("has_unknown") is True


def test_r07_unknown_recovery_adopts_by_same_client_id_without_duplicate_post(env):
    service, broker = env
    broker.submit_order = Mock(side_effect=_http_500_api_error())
    result = service.execute(trade_intent=opening(), dollar_amount=1000)
    client_oid = result["orders"][0]["client_order_id"]
    assert result["orders"][0]["status"] == "UNKNOWN"
    # The broker had in fact accepted the parent: it now shows up under the
    # SAME client_order_id. Recovery must adopt it, never re-POST.
    broker.orders.append(NS(
        id="parent-x", client_order_id=client_oid, symbol="AAPL", side="buy",
        qty=9, filled_qty=0, filled_avg_price=None, status="accepted",
        updated_at=now(), legs=[], notional=None,
    ))
    recovery = service.startup_recover()
    assert broker.submit_order.call_count == 1, "adoption must not duplicate the POST"
    row = service.store.get_order_by_client(client_oid)
    assert row["status"] == "ACCEPTED" and row["broker_order_id"] == "parent-x", row
    assert recovery["success"] and recovery["account_execution_state"] == "CLEAN", recovery


# ---------------------------------------------------------------------------
# R09 — a program-protected position that lost its protection pauses
# ---------------------------------------------------------------------------


def test_r09_missing_protection_pauses_existing_program_position(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    assert broker.qty == 9
    # The broker/exchange/operator cancels both protective children while the
    # filled exposure remains.
    for child in broker.orders[0].legs:
        child.status = "canceled"
    result = service.startup_recover()
    assert result["success"] is False
    assert result["account_execution_state"] == "PAUSED", result
    assert any(
        r.startswith("PROTECTION_GAP:") for r in result["reconciliation_reasons"]
    ), result
    assert broker.qty == 9


def test_r09_closed_position_clears_protection_gap(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    for child in broker.orders[0].legs:
        child.status = "canceled"
    assert service.startup_recover()["account_execution_state"] == "PAUSED"
    # The protective stop then fills: the position is genuinely closed and
    # every fill is explainable, so fresh facts may clear the pause.
    stop = broker.orders[0].legs[0]
    stop.status = "filled"
    stop.filled_qty = stop.qty
    stop.filled_avg_price = 100
    broker.qty = 0
    result = service.startup_recover()
    assert result["success"] and result["account_execution_state"] == "CLEAN", result


def test_r09_live_program_close_can_cover_exit(env):
    service, broker = env
    assert service.execute(trade_intent=opening(), dollar_amount=1000)["success"]
    for child in broker.orders[0].legs:
        child.status = "canceled"
    assert service.startup_recover()["account_execution_state"] == "PAUSED"
    # A program-owned live close covering the full position proves a safe
    # exit is already in progress: coverage is sufficient, no gap.
    did = "dec-r09-close"
    close_coid = client_order_id_for(did, "AAPL", "sell", role="close", seq=0)
    _, rows, _ = service.store.create_outbox(
        decision_id=did, run_id=None, symbol="AAPL", action="SELL",
        target_position="NEUTRAL",
        payload_json=json.dumps({"symbol": "AAPL", "action": "SELL", "kind": "liquidation"}),
        orders=[{"client_order_id": close_coid, "symbol": "AAPL", "side": "sell",
                 "quantity": 9, "notional": None}],
    )
    service.store.transition_order(rows[0]["order_id"], "ACCEPTED", broker_order_id="b-r09-close")
    broker.orders.append(NS(
        id="b-r09-close", client_order_id=close_coid, symbol="AAPL", side="sell",
        qty=9, filled_qty=0, filled_avg_price=None, status="new",
        updated_at=now(), legs=[], notional=None,
    ))
    result = service.startup_recover()
    assert result["success"] and result["account_execution_state"] == "CLEAN", result


# ---------------------------------------------------------------------------
# R14 — a stale position transition never becomes a blind flip
# ---------------------------------------------------------------------------


def test_r14_opposite_actual_position_blocks_stale_open(env):
    service, broker = env
    data = opening("LONG", "NEUTRAL")  # decided on flat facts...
    broker.qty = -5                    # ...but the broker now holds a SHORT
    result = service.execute(trade_intent=data, dollar_amount=1000, allow_shorts=True)
    assert result["success"] is False
    assert result.get("stale_position_transition") is True, result
    assert result.get("fail_closed") is True
    assert result["broker_calls"] == 0 and len(broker.submits) == 0
    assert broker.qty == -5, "the broker position must be untouched"


def test_r14_normal_matching_position_still_executes(env):
    service, broker = env
    data = opening("LONG", "NEUTRAL")
    result = service.execute(trade_intent=data, dollar_amount=1000)
    assert result["success"] and not result.get("paused"), result
    assert len(broker.submits) == 1
    assert broker.qty == 9
